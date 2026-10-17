#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# scripts/adhoc/metrics_probe.sh — read the custom business metrics back out
#
# WHEN THIS WAS USED
#   2026-10-17, when verifying that the app's custom metrics actually landed in
#   CloudWatch with the right dimensions, and again after the degradation drill
#   to confirm TriageFallback had counted the failure.
#
# THE GOTCHA THIS EXISTS TO REMEMBER
#   Every metric is published with a dimension (Environment=level1). A
#   `get-metric-statistics` call that omits `--dimensions` returns an EMPTY
#   result — not an error, just no datapoints — which looks exactly like "the
#   metric was never emitted". It is worth a wasted afternoon. Also:
#   `list-metrics` lags ~1 minute for a metric name that was just created, so a
#   brand-new metric can be missing from the list while its datapoints exist.
#
# HOW TO RUN
#   ./metrics_probe.sh                 # all metrics, last 3 hours
#   HOURS=24 ./metrics_probe.sh
#   METRIC=TriageFallback ./metrics_probe.sh
#   ENV=level1 REASON=client_error ./metrics_probe.sh   # TriageFallbackByReason
# ---------------------------------------------------------------------------
set -euo pipefail

NAMESPACE="${NAMESPACE:-IncidentTriage}"
ENV_NAME="${ENV:-level1}"
HOURS="${HOURS:-3}"
REGION="${AWS_REGION:-us-east-1}"

start="$(date -u -d "-${HOURS} hours" +%Y-%m-%dT%H:%M:%SZ)"
end="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

metrics=("${METRIC:-TriageSuccess}" "${METRIC:-BedrockLatencyMs}" "${METRIC:-TokensUsed}" \
         "${METRIC:-TriageFallback}" "${METRIC:-TriageFallbackByReason}")

echo "== $NAMESPACE  (Environment=$ENV_NAME, $start .. $end) =="

for m in "${metrics[@]}"; do
  dims="Name=Environment,Value=$ENV_NAME"
  if [ "$m" = "TriageFallbackByReason" ]; then
    dims="$dims Name=Reason,Value=${REASON:-client_error}"
  fi
  echo "-- $m   [$dims]"
  # $dims is intentionally unquoted so the two dimensions arrive as one argument each.
  aws cloudwatch get-metric-statistics --region "$REGION" \
    --namespace "$NAMESPACE" --metric-name "$m" \
    --dimensions $dims \
    --start-time "$start" --end-time "$end" --period 3600 \
    --statistics Sum Average Maximum SampleCount \
    --query "sort_by(Datapoints,&Timestamp)[].[Timestamp,Sum,Average,Maximum,SampleCount]" \
    --output text || echo "   (query failed)"
done

echo
echo "Explain the shape of what you just read:"
echo "  TriageSuccess      Sum = triaged incidents in the window"
echo "  TriageFallback     Sum = degraded (202) responses, by reason from ...ByReason"
echo "  BedrockLatencyMs   Average/Maximum = model latency seen by the app"
echo "  TokensUsed         Sum = input+output tokens billed"
