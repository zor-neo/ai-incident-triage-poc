"""API contract tests: ingestion, degradation, retrieval, health, correlation IDs."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.bedrock_client import TriageUnavailable
from app.config import Settings
from app.main import create_app
from app.metrics import MetricsEmitter
from app.models import AiTriage, TriageMetrics
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


def test_incident_is_persisted_as_pending_before_the_ai_call(settings, repo):
    """FR-03: the sanitized row is durable and PENDING_TRIAGE before Bedrock runs."""
    observed: dict = {}

    class ObservingTriage:
        def triage(self, **kwargs):
            observed["rows"] = repo._table.scan()["Items"]
            observed["prompt_log"] = kwargs["error_log"]
            return (
                AiTriage(summary="s", probable_cause="c", first_action="a"),
                TriageMetrics(ai_latency_ms=1, input_tokens=2, output_tokens=3),
            )

    payload = {
        "service": "payment-worker",
        "environment": "prod",
        "severity": "ERROR",
        "error_log": f"AccessDenied for key {VALID_KEY}",
    }
    with _client(settings, repo, ObservingTriage(), _metrics(settings)) as client:
        response = client.post("/incidents", json=payload)

    assert response.status_code == 201
    rows = observed["rows"]
    assert len(rows) == 1, "the in-flight incident must already be stored"
    assert rows[0]["status"] == "PENDING_TRIAGE"
    assert rows[0]["incident_id"] == response.json()["incident_id"]
    assert rows[0]["sanitized_log"] == observed["prompt_log"]
    assert VALID_KEY not in observed["prompt_log"]


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


def test_internal_config_is_absent_without_a_key(settings, repo, fake_triage):
    """No INTERNAL_API_KEY injected -> the endpoint does not exist (no auth bypass)."""
    with _client(settings, repo, fake_triage, _metrics(settings)) as client:
        response = client.get("/internal/config")
    assert response.status_code == 404


def test_internal_config_requires_the_injected_key(settings, repo, fake_triage):
    injected = settings.model_copy(update={"internal_api_key": "s3cret-from-ssm"})
    with _client(injected, repo, fake_triage, _metrics(injected)) as client:
        missing = client.get("/internal/config")
        wrong = client.get("/internal/config", headers={"X-Internal-Key": "nope"})
        ok = client.get("/internal/config", headers={"X-Internal-Key": "s3cret-from-ssm"})

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert ok.status_code == 200
    body = ok.json()
    assert body["internal_api_key_source"] == "injected_at_runtime"
    assert body["table_name"] == settings.table_name
    assert body["environment"] == "test"
    # The secret itself must never be reflected back.
    assert "s3cret-from-ssm" not in ok.text
