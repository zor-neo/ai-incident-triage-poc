#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# scripts/adhoc/triage_probe.sh — POST one realistic incident and show the row
#
# WHEN THIS WAS USED
#   2026-10-17, to capture first-hand live evidence for the README, the runbook
#   and docs/evidence/level1-deployment-transcript.md: a real Bedrock triage
#   (201 TRIAGED, ~500 ms, ~230 input tokens) and the exact stored record with
#   the fake AWS key redacted out of the log text.
#
# WHAT IT PROVES
#   The whole path works end to end against the deployed stack: sanitize ->
#   persist PENDING_TRIAGE -> InvokeModel -> mark TRIAGED -> GET it back, with
#   the key/email/IP scrubbed before storage. It is `smoke_test.sh` with the
#   full stored row printed, which is more useful when you are looking at the
#   document in DynamoDB rather than asserting a status code.
#
# HOW TO RUN
#   ./triage_probe.sh                     # BASE_URL auto-detected from the stack
#   BASE_URL=http://... ./triage_probe.sh
#
# COST: one Bedrock call (fractions of a cent on Nova Micro) and one table write.
# Uses an obviously fake key (AKIAIOSFODNN7EXAMPLE) — never a real one.
# ---------------------------------------------------------------------------
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
STACK="${STACK:-IncidentTriage-level1}"
BASE_URL="${BASE_URL:-}"
BASE_URL="${BASE_URL%/}"

if [ -z "$BASE_URL" ]; then
  BASE_URL="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='ApplicationUrl'].OutputValue" --output text)"
fi

# `python` is not always on PATH (Git Bash on Windows ships `python3` or neither)
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python
export PYTHON

payload='{
  "service": "payment-worker",
  "environment": "prod",
  "severity": "ERROR",
  "error_log": "botocore.exceptions.ClientError: AccessDenied for key AKIAIOSFODNN7EXAMPLE from 10.20.2.9 on arn:aws:secretsmanager:us-east-1:123456789012:secret:prod/payment/db-pass (oncall@example.com)"
}'

echo "== POST $BASE_URL/incidents"
body="$(mktemp)"
code="$(curl -sS -o "$body" -w '%{http_code}' -X POST "$BASE_URL/incidents" \
  -H 'Content-Type: application/json' -d "$payload")"
incident_id="$("$PYTHON" -c "import json,sys; print(json.load(open(sys.argv[1])).get('incident_id',''))" "$body")"
echo "HTTP $code"

echo "-- stored record"
curl -fsS "$BASE_URL/incidents/$incident_id" | "$PYTHON" -m json.tool
rm -f "$body"

echo
echo "Expected: HTTP 201 and status TRIAGED, with [REDACTED_AWS_KEY], [REDACTED_IP]"
echo "and [REDACTED_EMAIL] in sanitized_log - while the secret's resource PATH"
echo "(secret:prod/payment/db-pass) survives, because that is the useful part."
echo "A 202 / AI_UNAVAILABLE is also a pass: it means the model was throttled or"
echo "unreachable and the incident was still stored (see drill_ai_unavailable.sh)."
