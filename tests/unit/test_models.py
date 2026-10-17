"""Pydantic contract validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import (
    AiTriage,
    IncidentCreate,
    IncidentResponse,
    IncidentStatus,
    Severity,
    utc_now_iso,
)


def test_incident_create_defaults():
    incident = IncidentCreate(service="payment-worker", error_log="boom")
    assert incident.environment == "prod"
    assert incident.severity is Severity.ERROR


def test_incident_create_rejects_unknown_severity():
    with pytest.raises(ValidationError):
        IncidentCreate(service="svc", severity="PANIC", error_log="boom")


def test_incident_create_rejects_empty_and_oversized_logs():
    with pytest.raises(ValidationError):
        IncidentCreate(service="svc", error_log="")
    with pytest.raises(ValidationError):
        IncidentCreate(service="svc", error_log="x" * 16001)


def test_incident_response_round_trip():
    now = utc_now_iso()
    response = IncidentResponse(
        incident_id="inc_01J9Z8X7Y6W5V4U3T2S1R0",
        request_id="req_9f8e7d6c5b4a",
        service="payment-worker",
        environment="prod",
        severity=Severity.ERROR,
        status=IncidentStatus.TRIAGED,
        sanitized_log="redacted",
        ai_triage=AiTriage(summary="s", probable_cause="c", first_action="a"),
        created_at=now,
        updated_at=now,
    )
    dumped = response.model_dump(mode="json")
    assert dumped["status"] == "TRIAGED"
    assert dumped["ai_triage"]["summary"] == "s"


def test_ai_triage_requires_non_empty_fields():
    with pytest.raises(ValidationError):
        AiTriage(summary="", probable_cause="c", first_action="a")
