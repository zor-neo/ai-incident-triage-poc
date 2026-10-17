"""Bedrock wrapper: response parsing, failure classification, and timeout config."""

from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from app.bedrock_client import TriageClient, TriageUnavailable
from app.config import Settings


class _FakeBody:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


class _FakeBedrock:
    """Mimics bedrock-runtime invoke_model, optionally raising."""

    def __init__(self, payload: dict | None = None, exc: Exception | None = None) -> None:
        self.payload = payload
        self.exc = exc
        self.last_call: dict | None = None

    def invoke_model(self, **kwargs):
        self.last_call = kwargs
        if self.exc is not None:
            raise self.exc
        return {"body": _FakeBody(self.payload or {})}


def _nova_payload(text: str, input_tokens: int = 100, output_tokens: int = 60) -> dict:
    return {
        "output": {"message": {"content": [{"text": text}]}},
        "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens},
        "stopReason": "end_turn",
    }


def _settings() -> Settings:
    return Settings(environment="test", metrics_enabled=False, git_sha="test-sha")


def test_successful_triage_parses_json_and_usage():
    body = json.dumps(
        {"summary": "s", "probable_cause": "c", "first_action": "a"}
    )
    fake = _FakeBedrock(payload=_nova_payload(body))
    client = TriageClient(_settings(), client=fake)

    triage, metrics = client.triage("svc", "ERROR", "prod", "log line")

    assert triage.summary == "s"
    assert metrics.input_tokens == 100
    assert metrics.output_tokens == 60
    assert metrics.ai_latency_ms is not None
    assert fake.last_call["modelId"] == "amazon.nova-micro-v1:0"


def test_markdown_fenced_json_is_parsed():
    fenced = '```json\n{"summary":"s","probable_cause":"c","first_action":"a"}\n```'
    client = TriageClient(_settings(), client=_FakeBedrock(payload=_nova_payload(fenced)))
    triage, _ = client.triage("svc", "ERROR", "prod", "log")
    assert triage.first_action == "a"


def test_invalid_json_raises_invalid_response():
    client = TriageClient(_settings(), client=_FakeBedrock(payload=_nova_payload("not json")))
    with pytest.raises(TriageUnavailable) as exc:
        client.triage("svc", "ERROR", "prod", "log")
    assert exc.value.reason == "invalid_response"


def test_missing_keys_raises_invalid_response():
    body = json.dumps({"summary": "only summary"})
    client = TriageClient(_settings(), client=_FakeBedrock(payload=_nova_payload(body)))
    with pytest.raises(TriageUnavailable) as exc:
        client.triage("svc", "ERROR", "prod", "log")
    assert exc.value.reason == "invalid_response"


def test_throttling_is_classified():
    error = ClientError({"Error": {"Code": "ThrottlingException"}}, "InvokeModel")
    client = TriageClient(_settings(), client=_FakeBedrock(exc=error))
    with pytest.raises(TriageUnavailable) as exc:
        client.triage("svc", "ERROR", "prod", "log")
    assert exc.value.reason == "throttled"
    assert exc.value.metrics is not None


def test_access_denied_is_classified():
    error = ClientError({"Error": {"Code": "AccessDeniedException"}}, "InvokeModel")
    client = TriageClient(_settings(), client=_FakeBedrock(exc=error))
    with pytest.raises(TriageUnavailable) as exc:
        client.triage("svc", "ERROR", "prod", "log")
    assert exc.value.reason == "access_denied"


def test_timeout_is_classified():
    error = ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")
    client = TriageClient(_settings(), client=_FakeBedrock(exc=error))
    with pytest.raises(TriageUnavailable) as exc:
        client.triage("svc", "ERROR", "prod", "log")
    assert exc.value.reason == "timeout"


def test_client_is_configured_with_hard_timeouts():
    client = TriageClient(_settings())
    config = client._client.meta.config
    assert config.connect_timeout == 2.0
    assert config.read_timeout == 5.0
    retries = config.retries or {}
    assert retries.get("total_max_attempts") == 1
