"""DynamoDB persistence behaviour against a moto-mocked table."""

from __future__ import annotations

from app.models import AiTriage, IncidentCreate, IncidentStatus, TriageMetrics
from app.repository import FALLBACK_MESSAGE, IncidentRepository


def _incident() -> IncidentCreate:
    return IncidentCreate(
        service="payment-worker", environment="prod", severity="ERROR", error_log="redacted log"
    )


def test_create_pending_persists_raw_state(repo: IncidentRepository):
    item = repo.create_pending(
        incident=_incident(),
        sanitized_log="redacted log",
        incident_id="inc_TEST01",
        request_id="req_test",
    )
    assert item["status"] == IncidentStatus.PENDING_TRIAGE.value
    assert item["created_at"] == item["updated_at"]

    stored = repo.get("inc_TEST01")
    assert stored is not None
    assert stored["service"] == "payment-worker"
    assert stored["severity"] == "ERROR"


def test_mark_triaged_updates_status_and_triage(repo: IncidentRepository):
    repo.create_pending(_incident(), "redacted log", "inc_TEST02", "req_test")
    triage = AiTriage(summary="s", probable_cause="c", first_action="a")
    repo.mark_triaged(
        "inc_TEST02", triage, TriageMetrics(ai_latency_ms=780, input_tokens=112, output_tokens=68)
    )

    stored = repo.get("inc_TEST02")
    assert stored["status"] == IncidentStatus.TRIAGED.value
    assert stored["ai_triage"]["probable_cause"] == "c"
    assert stored["metrics"] == {"ai_latency_ms": 780, "input_tokens": 112, "output_tokens": 68}


def test_mark_ai_unavailable_with_metrics(repo: IncidentRepository):
    repo.create_pending(_incident(), "redacted log", "inc_TEST03", "req_test")
    repo.mark_ai_unavailable(
        "inc_TEST03", FALLBACK_MESSAGE, TriageMetrics(ai_latency_ms=5000)
    )

    stored = repo.get("inc_TEST03")
    assert stored["status"] == IncidentStatus.AI_UNAVAILABLE.value
    assert stored["message"] == FALLBACK_MESSAGE
    assert stored["metrics"] == {"ai_latency_ms": 5000}
    assert "ai_triage" not in stored


def test_mark_ai_unavailable_without_metrics(repo: IncidentRepository):
    repo.create_pending(_incident(), "redacted log", "inc_TEST04", "req_test")
    repo.mark_ai_unavailable("inc_TEST04")

    stored = repo.get("inc_TEST04")
    assert stored["status"] == IncidentStatus.AI_UNAVAILABLE.value
    assert "metrics" not in stored


def test_get_missing_incident_returns_none(repo: IncidentRepository):
    assert repo.get("inc_DOES_NOT_EXIST") is None
