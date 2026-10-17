#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# scripts/adhoc/drill_ai_unavailable.sh — prove graceful degradation in prod
#
# WHEN THIS WAS USED
#   2026-10-17, once, to validate the 202 path against the real deployment: the
#   model id in /etc/incident-triage.env was pointed at a non-existent model and
#   the service was restarted, the API was probed, and the file was restored.
#   The incident was NOT lost: it was stored as AI_UNAVAILABLE with the raw
#   sanitized log, the app logged `bedrock_invoke_failed` (ValidationException,
#   54 ms - "The provided model identifier is invalid") and `triage_fallback`,
#   and the TriageFallback metric counted it.
#
# WHAT IT PROVES
#   The resilience contract in the README: a model problem produces a stored
#   incident and a 202, never a 5xx and never data loss.
#
# WHY IT EDITS A FILE OVER SSM INSTEAD OF REDEPLOYING
#   This is what was actually done. BEDROCK_MODEL_ID is a CDK context parameter,
#   so the "proper" way to change it is a deploy - but a deploy replaces the
#   instance (ADR-0006) and takes ~4 minutes twice over for a 30-second check.
#   Editing the EnvironmentFile and restarting the unit exercises the identical
#   code path in seconds. The file is backed up and restored on every exit path,
#   including Ctrl-C.
#
# HOW TO RUN
#   ./drill_ai_unavailable.sh --confirm-degradation
#   BAD_MODEL_ID=amazon.nova-does-not-exist-v1:0 ./drill_ai_unavailable.sh --confirm-degradation
#
# WHAT TO EXPECT
#   A 202 with status AI_UNAVAILABLE. The logged error is a ValidationException
#   ("the provided model identifier is invalid") because Bedrock rejects the
#   identifier before the IAM policy scoped to the real model matters; an
#   AccessDenied would land in the same `client_error` fallback path. The
#   assertion is the 202, not the error class.
# ---------------------------------------------------------------------------
set -euo pipefail

if [ "${1:-}" != "--confirm-degradation" ]; then
  sed -n '2,38p' "$0"
  echo
  echo "refusing to degrade a live stack without --confirm-degradation" >&2
  exit 2
fi

REGION="${AWS_REGION:-us-east-1}"
STACK="${STACK:-IncidentTriage-level1}"
BAD_MODEL_ID="${BAD_MODEL_ID:-amazon.nova-does-not-exist-v1:0}"
ENV_FILE="${ENV_FILE:-/etc/incident-triage.env}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSM="$SCRIPT_DIR/instance_ssm.sh"

BASE_URL="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ApplicationUrl'].OutputValue" --output text)"

restore() {
  echo "== restoring $ENV_FILE and restarting the service"
  "$SSM" raw "if [ -f ${ENV_FILE}.drill-bak ]; then mv ${ENV_FILE}.drill-bak ${ENV_FILE}; systemctl restart incident-triage; fi; grep '^BEDROCK_MODEL_ID=' ${ENV_FILE}"
}
trap restore EXIT

probe() {
  local label="$1"
  echo "-- probe ($label)"
  curl -sS -o /tmp/drill.json -w '   HTTP %{http_code}\n' -X POST "$BASE_URL/incidents" \
    -H 'Content-Type: application/json' \
    -d '{"service":"payment-worker","environment":"prod","severity":"ERROR","error_log":"DEBUG forced failure drill"}'
  python -c "import json;d=json.load(open('/tmp/drill.json'));print('   status  =',d.get('status'));print('   message =',d.get('message'))" 2>/dev/null \
    || cat /tmp/drill.json
}

echo "== 1/3 point the service at $BAD_MODEL_ID (backup first, restore on exit) =="
"$SSM" raw "cp ${ENV_FILE} ${ENV_FILE}.drill-bak && sed -i 's|^BEDROCK_MODEL_ID=.*|BEDROCK_MODEL_ID=${BAD_MODEL_ID}|' ${ENV_FILE} && systemctl restart incident-triage && sleep 2 && grep '^BEDROCK_MODEL_ID=' ${ENV_FILE}"

echo "== 2/3 assert the degraded path =="
probe "expect 202 AI_UNAVAILABLE, incident still stored"
echo "-- the same evidence in the app log:"
"$SSM" raw "journalctl -u incident-triage --since '2 min ago' --no-pager | grep -E 'bedrock_invoke_failed|triage_fallback'" || true
HOURS=1 "$SCRIPT_DIR/metrics_probe.sh" 2>/dev/null | grep -A2 'TriageFallback ' || true

echo "== 3/3 restore (the trap does this too, even on failure) =="
restore
trap - EXIT
probe "expect 201 TRIAGED again"

echo
echo "Done. The point of the drill is the evidence - put what you saw in the"
echo "incident record or the deployment transcript."
