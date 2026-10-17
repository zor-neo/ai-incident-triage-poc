"""DynamoDB persistence (FR-03, FR-05, FR-06).

Raw incident state is written *before* the AI call so an incident is never lost
when Bedrock degrades. DynamoDB returns numbers as ``Decimal``; ``_to_plain``
converts them back so FastAPI/Pydantic can serialize responses.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import boto3
from ulid import ULID

from app.models import (
    AiTriage,
    IncidentCreate,
    IncidentStatus,
    TriageMetrics,
    utc_now_iso,
)

FALLBACK_MESSAGE = (
    "AI triage unavailable; inspect raw sanitized_log and service CloudWatch logs directly."
)


def new_incident_id() -> str:
    return f"inc_{ULID()}"


def new_request_id() -> str:
    return f"req_{str(ULID())[-12:].lower()}"


def _to_plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_plain(v) for v in value]
    return value


def _metrics_dict(metrics: TriageMetrics | None) -> dict[str, int] | None:
    if metrics is None:
        return None
    return {
        k: v
        for k, v in {
            "ai_latency_ms": metrics.ai_latency_ms,
            "input_tokens": metrics.input_tokens,
            "output_tokens": metrics.output_tokens,
        }.items()
        if v is not None
    }


class IncidentRepository:
    def __init__(self, table_name: str, region: str, endpoint_url: str | None = None) -> None:
        resource = boto3.resource("dynamodb", region_name=region, endpoint_url=endpoint_url)
        self._table = resource.Table(table_name)
        self.table_name = table_name

    def create_pending(
        self,
        incident: IncidentCreate,
        sanitized_log: str,
        incident_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        item: dict[str, Any] = {
            "incident_id": incident_id,
            "request_id": request_id,
            "service": incident.service,
            "environment": incident.environment,
            "severity": incident.severity.value,
            "status": IncidentStatus.PENDING_TRIAGE.value,
            "sanitized_log": sanitized_log,
            "created_at": now,
            "updated_at": now,
        }
        self._table.put_item(Item=item)
        return item

    def mark_triaged(
        self, incident_id: str, triage: AiTriage, metrics: TriageMetrics | None
    ) -> None:
        self._table.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression=(
                "SET #s = :status, ai_triage = :triage, #m = :metrics, updated_at = :now"
            ),
            ExpressionAttributeNames={"#s": "status", "#m": "metrics"},
            ExpressionAttributeValues={
                ":status": IncidentStatus.TRIAGED.value,
                ":triage": triage.model_dump(),
                ":metrics": _metrics_dict(metrics) or {},
                ":now": utc_now_iso(),
            },
        )

    def mark_ai_unavailable(
        self,
        incident_id: str,
        message: str = FALLBACK_MESSAGE,
        metrics: TriageMetrics | None = None,
    ) -> None:
        update = "SET #s = :status, message = :message, updated_at = :now"
        values: dict[str, Any] = {
            ":status": IncidentStatus.AI_UNAVAILABLE.value,
            ":message": message,
            ":now": utc_now_iso(),
        }
        metrics_payload = _metrics_dict(metrics)
        if metrics_payload:
            update += ", #m = :metrics"
            values[":metrics"] = metrics_payload
            names = {"#s": "status", "#m": "metrics"}
        else:
            names = {"#s": "status"}
        self._table.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression=update,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def get(self, incident_id: str) -> dict[str, Any] | None:
        response = self._table.get_item(Key={"incident_id": incident_id})
        item = response.get("Item")
        return _to_plain(item) if item else None
