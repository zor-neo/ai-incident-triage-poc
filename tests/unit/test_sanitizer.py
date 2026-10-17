"""Sanitizer coverage: each pattern, ordering, and false-positive guards."""

from __future__ import annotations

import pytest

from app.sanitizer import (
    REDACTED_AWS_KEY,
    REDACTED_AWS_SECRET,
    REDACTED_EMAIL,
    REDACTED_IP,
    REDACTED_SECRET,
    sanitize,
)


def test_aws_access_key_is_redacted():
    result = sanitize("key AKIAIOSFODNN7EXAMPLE returned AccessDenied")
    assert "[REDACTED_AWS_KEY]" in result.text
    assert "AKIAIOSFODNN7EXAMPLE" not in result.text
    assert result.redactions["aws_access_key_id"] == 1


def test_temporary_session_key_is_redacted():
    result = sanitize("ASIAIOSFODNN7EXAMPLE leaked")
    assert REDACTED_AWS_KEY in result.text


def test_aws_secret_access_key_context_is_redacted():
    raw = "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    result = sanitize(raw)
    assert REDACTED_AWS_SECRET in result.text
    assert "wJalrXUtnFEMI" not in result.text


def test_bearer_token_is_redacted():
    result = sanitize("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc")
    assert REDACTED_SECRET in result.text
    assert "eyJhbGciOiJIUzI1NiJ9" not in result.text


def test_api_key_assignment_is_redacted():
    result = sanitize("api_key: sk_live_51Habcdefg")
    assert REDACTED_SECRET in result.text
    assert "sk_live_51Habcdefg" not in result.text


def test_email_is_redacted():
    result = sanitize("contact admin@example.com immediately")
    assert REDACTED_EMAIL in result.text
    assert "admin@example.com" not in result.text


def test_ipv4_is_redacted():
    result = sanitize("connection refused from 10.20.3.15:5432")
    assert REDACTED_IP in result.text
    assert "10.20.3.15" not in result.text


def test_multiple_rules_in_one_payload():
    raw = (
        "botocore ClientError for key AKIAIOSFODNN7EXAMPLE "
        "user justin@example.com from 192.168.1.50"
    )
    result = sanitize(raw)
    assert result.redactions == {
        "aws_access_key_id": 1,
        "email": 1,
        "ipv4": 1,
    }
    assert result.total_redactions == 3


@pytest.mark.parametrize(
    "text",
    [
        "Timeout waiting for connection to payment-gateway",
        "AKIA is a prefix, not a key",
        "NullPointerException at com.example.Service.handle(Service.java:42)",
        "GET /incidents returned 503 after 4 retries",
    ],
)
def test_benign_text_is_untouched(text: str):
    result = sanitize(text)
    assert result.text == text
    assert result.total_redactions == 0


def test_redaction_is_idempotent():
    once = sanitize("key AKIAIOSFODNN7EXAMPLE").text
    twice = sanitize(once).text
    assert once == twice
