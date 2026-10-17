#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# scripts/adhoc/rollback_lab.sh - the Level 2 "bad release" game day (INC-0004)
#
# WHEN THIS WAS USED
#   2026-10-18, against the live Level 2 staging service, to answer a question the
#   design asserts but had not been tested: *what actually happens to users when a
#   release that cannot become healthy is promoted?*
#
# WHAT IT DOES
#   1. builds an image with the same application bytes but no server process, so
#      neither the container health check nor the ALB health check can ever pass;
#   2. pushes it as its own immutable tag;
#   3. rolls it onto STAGING only (never prod) with scripts/deploy_ecs_image.py;
#   4. prints what the service, the target group and the alarm are doing;
#   5. rolls back to the last known-good tag.
#
# WHAT IT SHOWED (the point of the exercise)
#   * staging kept serving the previous version the whole time - zero downtime,
#     because ECS will not drain healthy tasks until replacements are healthy;
#   * the unhealthy-targets alarm went ALARM (about 3 minutes after the bad task
#     registered, the same CloudWatch lag measured in INC-0002);
#   * ECS removed the bad task itself ("Task failed ELB health checks");
#   * the deploy command exited non-zero, which is the signal a human acts on;
#   * rollback took ~4 minutes and needed no task editing.
#
# USAGE
#   scripts/adhoc/rollback_lab.sh                 # staging, automatic rollback
#   scripts/adhoc/rollback_lab.sh --keep-bad      # leave the bad release in place
#
# SAFETY: it refuses to touch anything but the staging service.
# ---------------------------------------------------------------------------
set -euo pipefail

SERVICE="incident-triage-staging"
CLUSTER="${CLUSTER_NAME:-incident-triage-level2}"
REGION="${AWS_REGION:-us-east-1}"
REPOSITORY="${ECR_REPOSITORY:-incident-triage}"
KEEP_BAD=0
[ "${1:-}" = "--keep-bad" ] && KEEP_BAD=1

if [ "$SERVICE" != "incident-triage-staging" ]; then
  echo "refusing to run: this lab only ever touches staging" >&2
  exit 2
fi

command -v aws >/dev/null 2>&1 || { echo "aws CLI not found" >&2; exit 1; }
PYTHON="${PYTHON:-python3}"; command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python

URI="$(aws ecr describe-repositories --repository-names "$REPOSITORY" --region "$REGION" \
        --query 'repositories[0].repositoryUri' --output text)"
GOOD_TAG="$(curl -fsS "$(aws cloudformation describe-stacks --stack-name IncidentTriage-level2 \
  --region "$REGION" --query "Stacks[0].Outputs[?OutputKey=='StagingUrl'].OutputValue" --output text)/health" \
  | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
BAD_TAG="lab-bad-$(date +%Y%m%d%H%M%S)"
STAGING_URL="$(aws cloudformation describe-stacks --stack-name IncidentTriage-level2 \
  --region "$REGION" --query "Stacks[0].Outputs[?OutputKey=='StagingUrl'].OutputValue" --output text)"
TG_ARN="$(aws elbv2 describe-target-groups --names "$SERVICE" --region "$REGION" \
  --query 'TargetGroups[0].TargetGroupArn' --output text)"

echo "== current good tag: $GOOD_TAG  (staging: $STAGING_URL)"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cat > "$work/Dockerfile" <<EOF
FROM ${URI}:${GOOD_TAG}
# Identical application bytes to the healthy release; the process simply never
# serves, so no health check can pass. This is the "bad release".
CMD ["sleep", "infinity"]
EOF

echo "== building the bad release as $BAD_TAG"
docker build --platform linux/amd64 --provenance=false --sbom=false \
  -t "${URI}:${BAD_TAG}" "$work" >/dev/null
docker push "${URI}:${BAD_TAG}" | tail -1

echo
echo "== promoting the bad release to staging (this is expected to fail)"
set +e
"$PYTHON" scripts/deploy_ecs_image.py --service "$SERVICE" --image-tag "$BAD_TAG" \
  --cluster "$CLUSTER" --region "$REGION" --timeout 240
deploy_rc=$?
set -e
echo "   deploy exited $deploy_rc (non-zero is the correct outcome)"

echo
echo "== was staging ever degraded? (should still be $GOOD_TAG)"
curl -fsS "$STAGING_URL/health" || echo "   staging did not answer"

echo
echo "== target health"
aws elbv2 describe-target-health --target-group-arn "$TG_ARN" --region "$REGION" \
  --query 'TargetHealthDescriptions[].{state:TargetHealth.State,reason:TargetHealth.Reason}' \
  --output table

echo "== alarm"
aws cloudwatch describe-alarms --alarm-names "IncidentTriage-level2-staging-alb-unhealthy-targets" \
  --region "$REGION" --query 'MetricAlarms[].{state:StateValue,reason:StateReason}' --output json

if [ "$KEEP_BAD" -eq 1 ]; then
  echo
  echo "== --keep-bad: leaving revision on $BAD_TAG for inspection."
  echo "   roll back with: $PYTHON scripts/deploy_ecs_image.py --service $SERVICE --image-tag $GOOD_TAG"
  exit 0
fi

echo
echo "== rolling back to $GOOD_TAG"
"$PYTHON" scripts/deploy_ecs_image.py --service "$SERVICE" --image-tag "$GOOD_TAG" \
  --cluster "$CLUSTER" --region "$REGION" --timeout 420

echo
echo "== staging after rollback"
curl -fsS "$STAGING_URL/health"
echo
echo "lab complete: a bad release cost a failed deployment, not an outage."
