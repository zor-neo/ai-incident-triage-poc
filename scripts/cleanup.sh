#!/usr/bin/env bash
# FinOps teardown and orphan verification for the Level 1 stack.
#
# Usage:
#   scripts/cleanup.sh                # destroy the stack, then verify no orphans (exit 1 on a leak)
#   scripts/cleanup.sh --check        # verification only, destroys nothing (safe while the stack is up)
#   scripts/cleanup.sh --check <name> # same, for a different stack name
#
# Project-scoped checks must be EMPTY after a teardown. Region-wide inventory is
# printed for awareness only and never fails the run: this account also hosts
# earlier labs (a k8s-app ALB, CWAlarmTest, an abandoned ECS cluster) that this
# script must not be confused by.
#
# Two deliberate exclusions:
#   * EC2 instance ARNs are filtered out of the tag check, because terminated
#     instances stay visible in the tagging API for roughly an hour after a
#     replacement. Instance state is asserted separately, by state.
#   * There is no dedicated budgets check: an AWS Budget's name carries no project
#     identifier (`MonthlyBudget-us-east-1-...`), so it is caught by its Project tag.
set -euo pipefail

MODE="destroy"
POSITIONAL=()
for arg in "$@"; do
  case "$arg" in
    --check|--check-only) MODE="check" ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) POSITIONAL+=("$arg") ;;
  esac
done

STACK_NAME="${POSITIONAL[0]:-${STACK_NAME:-IncidentTriage-level1}}"
REGION="${AWS_REGION:-us-east-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRESENT=0

if [ "$MODE" = "destroy" ]; then
  echo "== destroying $STACK_NAME in $REGION =="
  ( cd "$SCRIPT_DIR/../infra" && uv run --no-sync cdk destroy "$STACK_NAME" --force )
else
  echo "== check mode: nothing will be destroyed ($STACK_NAME in $REGION) =="
fi

echo
echo "== project-scoped checks (a teardown must leave these empty) =="

# report <label> <findings> : counts and prints one check's findings.
# NB: called with a command substitution argument, not in a pipeline - a pipeline
# would run it in a subshell and lose the PRESENT increment.
report() {
  local label="$1" out="$2"
  if [ -n "$out" ]; then
    PRESENT=$((PRESENT + 1))
    printf '   %-30s %s\n' "$label" "$out"
  else
    printf '   %-30s %s\n' "$label" "none"
  fi
  return 0
}

# flatten text output and drop terminated-instance noise (they linger ~1h)
flat() { tr '\t' '\n' | { grep -v ':instance/' || true; } | tr '\n' ' '; }

report "tagged Project=IncidentTriage" "$(aws resourcegroupstaggingapi get-resources --region "$REGION" \
  --tag-filters Key=Project,Values=IncidentTriage \
  --query "ResourceTagMappingList[].ResourceARN" --output text 2>/dev/null | flat)"
report "NAT gateways" "$(aws ec2 describe-nat-gateways --region "$REGION" \
  --filter Name=tag:Project,Values=IncidentTriage Name=state,Values=available,pending \
  --query "NatGateways[].NatGatewayId" --output text 2>/dev/null | flat)"
report "load balancers" "$(aws elbv2 describe-load-balancers --region "$REGION" \
  --query "LoadBalancers[?contains(LoadBalancerName, 'Incide')].LoadBalancerName" --output text 2>/dev/null | flat)"
report "DynamoDB tables" "$(aws dynamodb list-tables --region "$REGION" \
  --query "TableNames[?starts_with(@, 'IncidentTriage')]" --output text 2>/dev/null | flat)"
report "SNS topics" "$(aws sns list-topics --region "$REGION" \
  --query "Topics[?contains(TopicArn, 'IncidentTriage')].TopicArn" --output text 2>/dev/null | flat)"
report "S3 buckets" "$(aws s3api list-buckets \
  --query "Buckets[?contains(Name, 'incidenttriage')].Name" --output text 2>/dev/null | flat)"
report "CloudWatch log groups" "$(aws logs describe-log-groups --region "$REGION" \
  --query "logGroups[?contains(logGroupName, 'incident-triage')].logGroupName" --output text 2>/dev/null | flat)"
report "EC2 instances (live)" "$(aws ec2 describe-instances --region "$REGION" \
  --filters Name=tag:Project,Values=IncidentTriage Name=instance-state-name,Values=running,stopped,pending \
  --query "Reservations[].Instances[].InstanceId" --output text 2>/dev/null | flat)"
report "EBS volumes (unattached)" "$(aws ec2 describe-volumes --region "$REGION" \
  --filters Name=tag:Project,Values=IncidentTriage \
  --query "Volumes[?State=='available'].VolumeId" --output text 2>/dev/null | flat)"
report "EBS snapshots" "$(aws ec2 describe-snapshots --owner-ids self --region "$REGION" \
  --filters Name=tag:Project,Values=IncidentTriage \
  --query "Snapshots[].SnapshotId" --output text 2>/dev/null | flat)"
report "CloudTrail trails" "$(aws cloudtrail describe-trails --region "$REGION" \
  --query "trailList[?contains(Name, 'IncidentTriage')].Name" --output text 2>/dev/null | flat)"
report "CloudWatch alarms" "$(aws cloudwatch describe-alarms --region "$REGION" \
  --query "MetricAlarms[?contains(AlarmName, 'IncidentTriage')].AlarmName" --output text 2>/dev/null | flat)"

echo
echo "== awareness: region-wide inventory (informational, never fails the run) =="
printf '   %-30s %s\n' "all NAT gateways" "$(aws ec2 describe-nat-gateways --region "$REGION" \
  --filter Name=state,Values=available,pending --query "NatGateways[].NatGatewayId" --output text 2>/dev/null)"
printf '   %-30s %s\n' "all load balancers" "$(aws elbv2 describe-load-balancers --region "$REGION" \
  --query "LoadBalancers[].LoadBalancerName" --output text 2>/dev/null)"
printf '   %-30s %s\n' "all running instances" "$(aws ec2 describe-instances --region "$REGION" \
  --filters Name=instance-state-name,Values=running --query "Reservations[].Instances[].InstanceId" --output text 2>/dev/null)"
printf '   %-30s %s\n' "CDKToolkit stack" "$(aws cloudformation describe-stacks --region "$REGION" \
  --stack-name CDKToolkit --query "Stacks[].StackStatus" --output text 2>/dev/null || echo "not deployed")"
printf '   %-30s %s\n' "bootstrap bucket" "$(aws s3api list-buckets \
  --query "Buckets[?contains(Name, 'cdk-hnb659fds-assets')].Name" --output text 2>/dev/null)"

echo
if [ "$MODE" = "check" ]; then
  echo "check mode: $PRESENT project-scoped group(s) currently exist."
  echo "(expected while the stack is up; run without --check to destroy and assert emptiness)"
  exit 0
fi

if [ "$PRESENT" -gt 0 ]; then
  echo "LEAK: $PRESENT project-scoped check(s) still returned resources - inspect the ARNs/IDs above."
  exit 1
fi

echo "Clean: every project-scoped check is empty. CDKToolkit and its asset bucket are"
echo "intentionally retained for future deploys (delete them only to fully de-bootstrap)."
