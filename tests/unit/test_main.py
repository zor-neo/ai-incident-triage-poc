"""API contract tests: ingestion, degradation, retrieval, health, correlation IDs."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.bedrock_client import TriageUnavailable
from app.config import Settings
from app.main import create_app
from app.metrics import MetricsEmitter
from app.models import TriageMetrics
from app.repository import FALLBACK_MESSAGE, IncidentRepository

VALID_KEY = "AKIAIOSFODNN7EXAMPLE"


def _client(
    settings: Settings, repo: IncidentRepository, triage, metrics: MetricsEmitter
) -> TestClient:
    app = create_app(settings=settings, repo=repo, triage=triage, metrics=metrics)
    return TestClient(app)


def _metrics(settings: Settings) -> MetricsEmitter:
    return MetricsEmitter(settings)


def test_health_endpoint(settings, repo, fake_triage):
    with _client(settings, repo, fake_triage, _metrics(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "version": "test-sha", "environment": "test"}


def test_post_incident_triaged_and_redacted(settings, repo, fake_triage):
    payload = {
        "service": "payment-worker",
        "environment": "prod",
        "severity": "ERROR",
        "error_log": f"AccessDenied for key {VALID_KEY} from 10.0.0.7 for admin@example.com",
    }
    with _client(settings, repo, fake_triage, _metrics(settings)) as client:
        response = client.post("/incidents", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "TRIAGED"
    assert body["ai_triage"]["summary"].startswith("Payment worker")
    assert body["metrics"]["input_tokens"] == 50
    assert body["incident_id"].startswith("inc_")
    assert "[REDACTED_AWS_KEY]" in body["sanitized_log"]
    assert VALID_KEY not in body["sanitized_log"]

    stored = repo.get(body["incident_id"])
    assert stored["status"] == "TRIAGED"
    assert fake_triage.calls[0]["error_log"] == body["sanitized_log"]


def test_post_incident_degrades_when_triage_unavailable(settings, repo):
    from tests.conftest import FakeTriageClient

    failing = FakeTriageClient(
        error=TriageUnavailable("timeout", "read timed out", TriageMetrics(ai_latency_ms=5000))
    )
    payload = {"service": "payments", "severity": "CRITICAL", "error_log": "boom"}
    with _client(settings, repo, failing, _metrics(settings)) as client:
        response = client.post("/incidents", json=payload)
        incident_id = response.json()["incident_id"]
        fetched = client.get(f"/incidents/{incident_id}")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "AI_UNAVAILABLE"
    assert body["ai_triage"] is None
    assert body["message"] == FALLBACK_MESSAGE
    assert body["metrics"]["ai_latency_ms"] == 5000

    assert fetched.status_code == 200
    assert fetched.json()["status"] == "AI_UNAVAILABLE"


def test_get_incident_not_found(settings, repo, fake_triage):
    with _client(settings, repo, fake_triage, _metrics(settings)) as client:
        response = client.get("/incidents/inc_MISSING")
    assert response.status_code == 404


def test_request_id_is_echoed_when_valid(settings, repo, fake_triage):
    with _client(settings, repo, fake_triage, _metrics(settings)) as client:
        response = client.post(
            "/incidents",
            json={"service": "svc", "severity": "INFO", "error_log": "ok"},
            headers={"X-Request-ID": "trace-abc-123"},
        )
    assert response.headers["X-Request-ID"] == "trace-abc-123"
    assert response.json()["request_id"] == "trace-abc-123"


def test_request_id_is_generated_when_invalid(settings, repo, fake_triage):
    with _client(settings, repo, fake_triage, _metrics(settings)) as client:
        response = client.post(
            "/incidents",
            json={"service": "svc", "severity": "INFO", "error_log": "ok"},
            headers={"X-Request-ID": "bad request id with spaces"},
        )
    generated = response.headers["X-Request-ID"]
    assert generated.startswith("req_")
    assert response.json()["request_id"] == generated


def test_invalid_payload_is_rejected(settings, repo, fake_triage):
    with _client(settings, repo, fake_triage, _metrics(settings)) as client:
        response = client.post("/incidents", json={"service": "svc"})
    assert response.status_code == 422
