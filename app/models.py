"""Pydantic v2 request/response and persistence contracts.

The JSON shapes here mirror the API contract in the technical specification
(sections 3.1 - 3.3) and the DynamoDB item model (section 5).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Severity(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class IncidentStatus(StrEnum):
    PENDING_TRIAGE = "PENDING_TRIAGE"
    TRIAGED = "TRIAGED"
    AI_UNAVAILABLE = "AI_UNAVAILABLE"


class IncidentCreate(BaseModel):
    service: str = Field(min_length=1, max_length=100)
    environment: str = Field(default="prod", min_length=1, max_length=50)
    severity: Severity = Severity.ERROR
    error_log: str = Field(min_length=1, max_length=16000)


class AiTriage(BaseModel):
    summary: str = Field(min_length=1)
    probable_cause: str = Field(min_length=1)
    first_action: str = Field(min_length=1)


class TriageMetrics(BaseModel):
    ai_latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class IncidentResponse(BaseModel):
    incident_id: str
    request_id: str
    service: str
    environment: str
    severity: Severity
    status: IncidentStatus
    sanitized_log: str
    ai_triage: AiTriage | None = None
    metrics: TriageMetrics | None = None
    message: str | None = None
    created_at: str
    updated_at: str


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
