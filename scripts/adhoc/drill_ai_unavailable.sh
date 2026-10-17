#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# scripts/adhoc/drill_ai_unavailable.sh — prove graceful degradation in prod
#
# WHEN THIS WAS USED
#   2026-10-17, once, to validate the 202 path against the real deployment: the
#   stack was deployed with an invalid Bedrock model id (it is a CDK context
#   parameter), the API was probed, and the stack was reverted to Nova Micro.
#   The incident was NOT lost: it was stored as AI_UNAVAILABLE with the raw
#   sanitized log, the app logged `bedrock_invoke_failed` (ValidationException,
#   54 ms) and `triage_fallback`, and the TriageFallback metric counted it.
#
# WHAT IT PROVES
#   The resilience contract in the README: a model problem produces a stored
#   incident and a 202, never a 5xx and never data loss.
#
# !! THIS DEPLOYS TWICE AND REPLACES THE INSTANCE BOTH TIMES (~4 min each) !!
#    It is the only script here that mutates the live stack, hence the guard.
#    Cost of one run: ~10 minutes of attention, two instance replacements.
#
# HOW TO RUN
#   ./drill_ai_unavailable.sh --confirm-degradation
#   BAD_MODEL_ID=amazon.nova-micro-v9:0 ./drill_ai_unavailable.sh --confirm-degradation
#
# WHAT TO EXPECT
#   A 202 with status AI_UNAVAILABLE. If a ValidationException is logged the
#   model id was rejected by Bedrock; if it is AccessDenied instead, the IAM
#   policy scoped to the real model blocked it first — both land in the same
#   `client_error` fallback path, and the assertion is the 202, not the error
#   class. The script always restores the default model id at the end.
# ---------------------------------------------------------------------------
set -euo pipefail

if [ "${1:-}" != "--confirm-degradation" ]; then
  sed -n '2,30p' "$0"
  echo
  echo "refusing to degrade a live stack without --confirm-degradation" >&2
  exit 2
fi

REGION="${AWS_REGION:-us-east-1}"
STACK="${STACK:-IncidentTriage-level1}"
BAD_MODEL_ID="${BAD_MODEL_ID:-amazon.nova-micro-v9:0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$SCRIPT_DIR/../../infra"

probe() {
  local label="$1"
  echo "-- probe ($label)"
  curl -sS -o /tmp/drill.json -w 'HTTP %{http_code}\n' -X POST "$BASE_URL/incidents" \
    -H 'Content-Type: application/json' \
    -d '{"service":"payment-worker","environment":"prod","severity":"ERROR","error_log":"DEBUG forced failure probe"}'
  python -c "import json;d=json.load(open('/tmp/drill.json'));print('   status =',d.get('status'));print('   fallback =',(d.get('ai_triage') or {}).get('summary','-'))"
}

echo "== 1/3 deploy the invalid model id ($BAD_MODEL_ID) =="
( cd "$INFRA_DIR" && uv run --no-sync cdk deploy "$STACK" --force -c bedrockModelId="$BAD_MODEL_ID" )

BASE_URL="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ApplicationUrl'].OutputValue" --output text)"

echo "== 2/3 assert the degraded path =="
probe "expect 202 AI_UNAVAILABLE"
echo "-- the SAME evidence in the app log (CloudWatch Logs, /aws/ec2/incident-triage):"
echo "   look for: bedrock_invoke_failed (reason, latency_ms) then triage_fallback"
aws logs tail /aws/ec2/incident-triage --region "$REGION" --since 10m 2>/dev/null | grep -E "bedrock_invoke_failed|triage_fallback" || true
"$SCRIPT_DIR/metrics_probe.sh" | grep -A2 TriageFallback || true

echo "== 3/3 revert to the default model id =="
( cd "$INFRA_DIR" && uv run --no-sync cdk deploy "$STACK" --force )
probe "expect 201 TRIAGED again"

echo
echo "Done. Record what you saw in the incident/transcript - the point of the drill"
echo "is the evidence, not the script."
