"""Shared fixtures: fake AWS credentials, moto-mocked DynamoDB, app factory."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from app.config import Settings
from app.models import AiTriage, TriageMetrics
from app.repository import IncidentRepository


@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        table_name="IncidentTriage-test",
        aws_region="us-east-1",
        metrics_enabled=False,
        git_sha="test-sha",
        log_level="WARNING",
    )


@pytest.fixture
def mock_aws_env():
    with mock_aws():
        yield


@pytest.fixture
def repo(mock_aws_env, settings: Settings) -> IncidentRepository:
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName=settings.table_name,
        KeySchema=[{"AttributeName": "incident_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "incident_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return IncidentRepository(
        table_name=settings.table_name, region="us-east-1", endpoint_url=None
    )


class FakeTriageClient:
    """Injectable stand-in for :class:`app.bedrock_client.TriageClient`."""

    def __init__(self, ai_triage: AiTriage | None = None, error: Exception | None = None) -> None:
        self.ai_triage = ai_triage or AiTriage(
            summary="Payment worker could not read its secret.",
            probable_cause="IAM role is missing secretsmanager:GetSecretValue.",
            first_action="Check CloudTrail for AccessDenied on GetSecretValue.",
        )
        self.error = error
        self.calls: list[dict] = []

    def triage(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.ai_triage, TriageMetrics(ai_latency_ms=120, input_tokens=50, output_tokens=40)


@pytest.fixture
def fake_triage() -> FakeTriageClient:
    return FakeTriageClient()
