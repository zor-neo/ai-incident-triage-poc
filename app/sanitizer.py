"""Regex-based PII / credential scrubbing.

Every inbound ``error_log`` passes through :func:`sanitize` before it reaches
structured logs, DynamoDB, or Amazon Bedrock (FR-02). Patterns are ordered from
most specific to most generic so contextual secrets are caught before the
generic token rule can partially consume them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from re import Pattern

REDACTED_AWS_KEY = "[REDACTED_AWS_KEY]"
# The placeholder strings below are redaction markers, not credentials.
REDACTED_AWS_SECRET = "[REDACTED_AWS_SECRET]"  # nosec B105
REDACTED_SECRET = "[REDACTED_SECRET]"  # nosec B105
REDACTED_EMAIL = "[REDACTED_EMAIL]"
REDACTED_IP = "[REDACTED_IP]"

# (name, pattern, replacement). Replacement may use backreferences.
_PATTERNS: list[tuple[str, Pattern[str], str]] = [
    (
        "aws_secret_access_key",
        re.compile(r"(?i)(aws_secret_access_key[\"'\s:=]+)([A-Za-z0-9/+=]{40})"),
        rf"\1{REDACTED_AWS_SECRET}",
    ),
    (
        "aws_access_key_id",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        REDACTED_AWS_KEY,
    ),
    (
        "bearer_or_api_key",
        re.compile(r"(?i)((?:bearer\s+|api[_-]?key\s*[:=]\s*))([a-z0-9\-._~+/]+=*)"),
        rf"\1{REDACTED_SECRET}",
    ),
    (
        "password_assignment",
        re.compile(r"(?i)((?:password|passwd|pwd|secret|token)\s*[:=]\s*[\"']?)([^\s\"'/]{6,})"),
        rf"\1{REDACTED_SECRET}",
    ),
    (
        "email",
        re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"),
        REDACTED_EMAIL,
    ),
    (
        "ipv4",
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        REDACTED_IP,
    ),
]


@dataclass(frozen=True)
class SanitizationResult:
    text: str
    redactions: dict[str, int] = field(default_factory=dict)

    @property
    def total_redactions(self) -> int:
        return sum(self.redactions.values())


def sanitize(raw: str) -> SanitizationResult:
    """Return the scrubbed text plus per-rule redaction counts."""
    text = raw
    counts: dict[str, int] = {}
    for name, pattern, replacement in _PATTERNS:
        text, replaced = pattern.subn(replacement, text)
        if replaced:
            counts[name] = counts.get(name, 0) + replaced
    return SanitizationResult(text=text, redactions=counts)
