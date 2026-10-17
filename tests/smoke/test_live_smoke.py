"""Live HTTP smoke tests (run against a deployed environment).

Executed by CI after a staging deploy, or locally:

    SMOKE_BASE_URL=http://my-alb-1234.us-east-1.elb.amazonaws.com uv run pytest tests/smoke
"""

from __future__ import annotations

import os

import httpx
import pytest

BASE_URL = os.environ.get("SMOKE_BASE_URL", "").rstrip("/")

pytestmark = pytest.mark.skipif(not BASE_URL, reason="SMOKE_BASE_URL is not set")

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def test_health_reports_version():
    response = httpx.get(f"{BASE_URL}/health", timeout=10)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["version"]


def test_ingest_then_retrieve_and_redact():
    payload = {
        "service": "smoke-test",
        "environment": "smoke",
        "severity": "ERROR",
        "error_log": f"AccessDenied for key {AWS_KEY} from 10.20.2.9 for oncall@example.com",
    }
    with httpx.Client(base_url=BASE_URL, timeout=15) as client:
        created = client.post("/incidents", json=payload, headers={"X-Request-ID": "pytest-smoke"})
        assert created.status_code in (201, 202), created.text

        body = created.json()
        assert body["status"] in {"TRIAGED", "AI_UNAVAILABLE"}
        assert AWS_KEY not in body["sanitized_log"]
        assert "[REDACTED_AWS_KEY]" in body["sanitized_log"]

        fetched = client.get(f"/incidents/{body['incident_id']}")
        assert fetched.status_code == 200
        assert fetched.json()["status"] == body["status"]


def test_unknown_incident_is_404():
    response = httpx.get(f"{BASE_URL}/incidents/inc_does_not_exist", timeout=10)
    assert response.status_code == 404
