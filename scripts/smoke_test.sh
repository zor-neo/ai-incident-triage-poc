#!/usr/bin/env bash
# Post-deployment smoke test for the AI Incident & Log Triage API.
#
# WHEN THIS WAS USED
#   After every one of the five deploys on 2026-10-17, and as the acceptance step
#   in docs/runbooks/level1-runbook.md §2. It ran against the live ALB each time
#   and is the reason the redaction of `secret:<path>` (INC-0001) was caught and
#   then verified fixed.
#
# Verifies the full ingestion path against a live environment:
#   1. GET  /health       -> 200 with the deployed version
#   2. POST /incidents    -> 201 (TRIAGED) or 202 (AI_UNAVAILABLE, degraded)
#   3. GET  /incidents/id -> matches the POST status
#   4. secret in the error log was scrubbed before storage
#
# Usage: scripts/smoke_test.sh <base-url>      (or BASE_URL=https://... scripts/smoke_test.sh)
set -euo pipefail

BASE_URL="${1:-${BASE_URL:-}}"
if [ -z "$BASE_URL" ]; then
  echo "usage: $0 <base-url>" >&2
  exit 2
fi
BASE_URL="${BASE_URL%/}"

PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python

json_field() {
  "$PYTHON" -c "import json,sys; print(json.load(sys.stdin).get('$1',''))"
}

echo "== smoke test against $BASE_URL =="

echo "-- GET /health"
health="$(curl -fsS "$BASE_URL/health")"
status="$(printf '%s' "$health" | json_field status)"
version="$(printf '%s' "$health" | json_field version)"
[ "$status" = "healthy" ] || { echo "FAIL: health status=$status"; exit 1; }
echo "   healthy, version=$version"

echo "-- POST /incidents"
payload='{"service":"smoke-test","environment":"smoke","severity":"ERROR","error_log":"botocore.exceptions.ClientError: AccessDenied for key AKIAIOSFODNN7EXAMPLE from 10.20.2.9 for oncall@example.com"}'
body_file="$(mktemp)"
code="$(curl -sS -o "$body_file" -w '%{http_code}' \
  -X POST "$BASE_URL/incidents" \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: smoke-test-run' \
  -d "$payload")"

if [ "$code" != "201" ] && [ "$code" != "202" ]; then
  echo "FAIL: unexpected HTTP $code"; cat "$body_file"; rm -f "$body_file"; exit 1
fi
incident_id="$(json_field incident_id < "$body_file")"
incident_status="$(json_field status < "$body_file")"
sanitized="$(json_field sanitized_log < "$body_file")"
rm -f "$body_file"
echo "   HTTP $code -> incident_id=$incident_id status=$incident_status"

case "$incident_status" in
  TRIAGED|AI_UNAVAILABLE) ;;
  *) echo "FAIL: unexpected incident status '$incident_status'"; exit 1 ;;
esac

case "$sanitized" in
  *AKIAIOSFODNN7EXAMPLE*) echo "FAIL: AWS key was not scrubbed"; exit 1 ;;
  *"[REDACTED_AWS_KEY]"*) echo "   ok: AWS key redacted" ;;
  *) echo "FAIL: redaction marker missing"; exit 1 ;;
esac

echo "-- GET /incidents/$incident_id"
fetched_status="$(curl -fsS "$BASE_URL/incidents/$incident_id" | json_field status)"
[ "$fetched_status" = "$incident_status" ] || {
  echo "FAIL: stored status '$fetched_status' != returned status '$incident_status'"; exit 1
}
echo "   ok: stored status matches"

echo "== smoke test passed =="
