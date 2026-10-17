"""Amazon Bedrock triage wrapper (FR-04, FR-05).

Hard timeouts (connect 2s / read 5s, single attempt) guarantee a slow model can
never exhaust upstream ALB connection pools. Every failure mode is converted
into a typed :class:`TriageUnavailable` so the API can degrade gracefully
instead of dropping the incident.
"""

from __future__ import annotations

import json
import time
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    ReadTimeoutError,
)

from app.config import Settings
from app.logging_config import get_logger
from app.models import AiTriage, TriageMetrics

SYSTEM_PROMPT = (
    "You are an on-call CloudOps triage assistant. "
    "Given a sanitized application error log, respond with ONLY a single JSON object "
    'with exactly these string keys: "summary", "probable_cause", "first_action". '
    "summary: one sentence describing what failed. "
    "probable_cause: the most likely root cause given the evidence. "
    "first_action: the single concrete first diagnostic step an on-call engineer should take. "
    "Do not include markdown fences, prose, or additional keys."
)


class TriageUnavailable(Exception):
    """Raised when triage cannot be produced; carries a coarse reason code."""

    def __init__(self, reason: str, detail: str = "", metrics: TriageMetrics | None = None):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.metrics = metrics


def _classify(exc: Exception) -> str:
    if isinstance(exc, (ConnectTimeoutError, ReadTimeoutError)):
        return "timeout"
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        throttling = {
            "ThrottlingException",
            "TooManyRequestsException",
            "ServiceQuotaExceededException",
        }
        denied = {"AccessDeniedException", "UnauthorizedException", "UnrecognizedClientException"}
        if code in throttling:
            return "throttled"
        if code in denied:
            return "access_denied"
        return "client_error"
    if isinstance(exc, BotoCoreError):
        return "connection_error"
    return "error"


def _extract_text(payload: dict[str, Any]) -> str:
    try:
        content = payload["output"]["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Bedrock response missing output.message.content") from exc
    parts = [block.get("text", "") for block in content if isinstance(block, dict)]
    text = "".join(parts).strip()
    if not text:
        raise ValueError("Bedrock response contained no text")
    return text


def _parse_triage(text: str) -> AiTriage:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("Triage JSON was not an object")
    missing = [k for k in ("summary", "probable_cause", "first_action") if not parsed.get(k)]
    if missing:
        raise ValueError(f"Triage JSON missing keys: {', '.join(missing)}")
    return AiTriage(
        summary=str(parsed["summary"]),
        probable_cause=str(parsed["probable_cause"]),
        first_action=str(parsed["first_action"]),
    )


class TriageClient:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        self._logger = get_logger("app.bedrock")
        self._client = client or boto3.client(
            "bedrock-runtime",
            region_name=settings.aws_region,
            config=Config(
                connect_timeout=settings.bedrock_connect_timeout_s,
                read_timeout=settings.bedrock_read_timeout_s,
                # total_max_attempts counts the initial call plus retries, so 1 means
                # "try once, never retry" - the 5s hard timeout must hold end-to-end.
                retries={"total_max_attempts": settings.bedrock_max_attempts, "mode": "standard"},
            ),
        )

    def _user_prompt(self, service: str, severity: str, environment: str, error_log: str) -> str:
        return (
            f"service: {service}\nenvironment: {environment}\nseverity: {severity}\n"
            f"sanitized_error_log:\n{error_log}"
        )

    def triage(
        self, service: str, severity: str, environment: str, error_log: str
    ) -> tuple[AiTriage, TriageMetrics]:
        body = {
            "system": [{"text": SYSTEM_PROMPT}],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"text": self._user_prompt(service, severity, environment, error_log)}
                    ],
                }
            ],
            "inferenceConfig": {
                "max_new_tokens": self._settings.bedrock_max_output_tokens,
                "temperature": self._settings.bedrock_temperature,
            },
        }

        started = time.perf_counter()
        try:
            response = self._client.invoke_model(
                modelId=self._settings.bedrock_model_id,
                body=json.dumps(body),
                accept="application/json",
                contentType="application/json",
            )
            payload = json.loads(response["body"].read())
        except Exception as exc:  # noqa: BLE001 - classified and re-raised as domain error
            latency_ms = int((time.perf_counter() - started) * 1000)
            reason = _classify(exc)
            metrics = TriageMetrics(ai_latency_ms=latency_ms)
            self._logger.warning(
                "bedrock_invoke_failed",
                reason=reason,
                error_type=type(exc).__name__,
                latency_ms=latency_ms,
            )
            raise TriageUnavailable(reason, str(exc), metrics) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
        metrics = TriageMetrics(
            ai_latency_ms=latency_ms,
            input_tokens=usage.get("inputTokens"),
            output_tokens=usage.get("outputTokens"),
        )

        try:
            triage = _parse_triage(_extract_text(payload))
        except (ValueError, json.JSONDecodeError) as exc:
            self._logger.warning("bedrock_response_invalid", error=str(exc), latency_ms=latency_ms)
            raise TriageUnavailable("invalid_response", str(exc), metrics) from exc

        self._logger.info(
            "bedrock_triaged",
            latency_ms=latency_ms,
            input_tokens=metrics.input_tokens,
            output_tokens=metrics.output_tokens,
        )
        return triage, metrics
