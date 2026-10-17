#!/usr/bin/env bash
# FinOps teardown and orphan verification for the Level 1 and Level 2 stacks.
#
# WHEN THIS WAS USED / WHY IT LOOKS LIKE THIS
#   2026-10-17: written at the end of the Level 1 build session. `--check` was run
#   while the stack was still up, which is the only way to prove the checks actually
#   see the live resources (they must find populated sections then, and none after a
#   destroy). That first run also exposed two bugs in this script that would
#   otherwise have produced a FALSE LEAK after teardown: a subshell that swallowed
#   the leak counter, and a filter that matched `/instance/` when ARNs read
#   `:instance/`. The current form is the tested one.
#   2026-10-18: extended for Level 2 - ECS, ECR and the out-of-band SSM parameters,
#   which are the first resources in this project that CDK does not own.
#
# Usage:
#   scripts/cleanup.sh                # destroy every stack, then verify no orphans (exit 1 on a leak)
#   scripts/cleanup.sh --check        # verification only, destroys nothing (safe while stacks are up)
#   scripts/cleanup.sh --check <name> # same, but only for one stack's destroy step
#
# Project-scoped checks must be EMPTY after a teardown. Region-wide inventory is
# printed for awareness only and never fails the run: this account also hosts
# earlier labs (a k8s-app ALB, CWAlarmTest, nginx-lab in ECR) that this script must
# not be confused by.
#
# Three deliberate exclusions:
#   * EC2 instance ARNs are filtered out of the tag check, because terminated
#     instances stay visible in the tagging API for roughly an hour after a
#     replacement. Instance state is asserted separately, by state.
#   * INACTIVE ECS task definitions are reported for awareness, never as a leak:
#     ECS task definitions cannot be deleted, only deregistered, and an inactive
#     revision costs nothing.
#   * There is no dedicated budgets check: an AWS Budget's name carries no project
#     identifier (`MonthlyBudget-us-east-1-...`), so it is caught by its Project tag.
set -euo pipefail

MODE="destroy"
POSITIONAL=()
for arg in "$@"; do
  case "$arg" in
    --check|--check-only) MODE="check" ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) POSITIONAL+=("$arg") ;;
  esac
done

REGION="${AWS_REGION:-us-east-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRESENT=0

# `uv` runs the CDK app (infra/cdk.json's "app" command), and it is frequently not
# on PATH in the shell that runs this script - a Windows install under
# /mnt/c/Users/*/AppData/Roaming/Python/Python3*/Scripts is invisible to WSL, and a
# WSL install is invisible to PowerShell. That is not fatal: the destroy step is
# verified against CloudFormation and falls back to the CloudFormation API, which
# needs only the AWS CLI. Using `cdk destroy` when it is available keeps the
# documented path, and the verification keeps it honest when it is not.
uv_search_paths=(
  "$HOME/.local/bin"
  "$HOME/.cargo/bin"
  /mnt/c/Users/*/AppData/Roaming/Python/Python3*/Scripts
  /c/Users/*/AppData/Roaming/Python/Python3*/Scripts
)
CDK_CMD=()
if ! command -v uv >/dev/null 2>&1; then
  for candidate in "${uv_search_paths[@]}"; do
    if [ -x "$candidate/uv" ] || [ -x "$candidate/uv.exe" ]; then
      export PATH="$PATH:$candidate"
      break
    fi
  done
fi
if command -v uv >/dev/null 2>&1; then
  CDK_CMD=(uv run --no-sync cdk)
else
  echo "note: 'uv' is not on PATH; destroying through the CloudFormation API instead"
  echo "      (identical effect - the removal policies are already in the templates)"
fi

stack_status() {
  aws cloudformation describe-stacks --stack-name "$1" --region "$REGION" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo ABSENT
}

# Destroy order matters: the services stack references the registry's image, so the
# services go first, then the registry that holds the images, then Level 1.
ALL_STACKS=(IncidentTriage-level2 IncidentTriage-level2-registry IncidentTriage-level1)
SSM_PREFIX="${SSM_PREFIX:-/incident-triage}"

if [ "$MODE" = "destroy" ]; then
  if [ "${#POSITIONAL[@]}" -gt 0 ]; then
    STACKS=("${POSITIONAL[@]}")
  else
    STACKS=("${ALL_STACKS[@]}")
  fi
  for stack in "${STACKS[@]}"; do
    echo "== destroying $stack in $REGION =="
    if [ "${#CDK_CMD[@]}" -gt 0 ]; then
      ( cd "$SCRIPT_DIR/../infra" && "${CDK_CMD[@]}" destroy "$stack" --force ) || true
    fi
    status="$(stack_status "$stack")"
    if [ "$status" != "ABSENT" ]; then
      echo "   CDK did not remove it (state: $status) - deleting via the CloudFormation API"
      aws cloudformation delete-stack --stack-name "$stack" --region "$REGION"
      aws cloudformation wait stack-delete-complete --stack-name "$stack" --region "$REGION" || true
      status="$(stack_status "$stack")"
    fi
    if [ "$status" = "ABSENT" ]; then
      echo "   $stack: gone"
  # Task definition revisions registered by the pipeline (scripts/deploy_ecs_image.py)
  # are NOT owned by CloudFormation, so deleting the stack deregisters only the
  # revisions CDK created - the pipeline's stay ACTIVE. They cost nothing, but they
  # are the same class of leftover as the SSM secret: created outside the stack, so
  # the stack cannot clean them up. Deregister them here.
  echo
  echo "== deregistering task definition revisions created outside CloudFormation =="
  deregistered=0
  for family in incident-triage-staging incident-triage-prod; do
    for arn in $(aws ecs list-task-definitions --family-prefix "$family" --status ACTIVE \
        --region "$REGION" --query 'taskDefinitionArns[]' --output text 2>/dev/null || true); do
      if aws ecs deregister-task-definition --task-definition "$arn" \
           --region "$REGION" >/dev/null 2>&1; then
        echo "   deregistered $arn"
        deregistered=$((deregistered + 1))
      fi
    done
  done
  [ "$deregistered" -gt 0 ] || echo "   none"
else
      echo "   WARNING: $stack still exists in state $status"
    fi
  done

  # The Level 2 secret is created out of band (ADR 0010), so no stack owns it and
  # cdk destroy cannot remove it. Leaving it would be a real orphan.
  echo
  echo "== removing the out-of-band secrets under $SSM_PREFIX =="
  params="$(aws ssm describe-parameters --region "$REGION" \
    --parameter-filters "Key=Name,Option=BeginsWith,Values=$SSM_PREFIX" \
    --query 'Parameters[].Name' --output text 2>/dev/null || true)"
  if [ -z "$params" ]; then
    echo "   none"
  else
    for name in $params; do
      aws ssm delete-parameter --name "$name" --region "$REGION" && echo "   deleted $name"
    done
  fi
else
  echo "== check mode: nothing will be destroyed ($REGION) =="
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

# The tagging API is eventually consistent: it keeps reporting deleted resources for
# minutes (sometimes longer). Types that already have an authoritative direct check
# below are therefore filtered out of the catch-all tag check - otherwise a teardown
# looks like a leak because of an index that has not caught up. Everything the tag
# check still reports is a genuine leftover, and every resource type it can see
# without a dedicated check has been verified this way.
COVERED_TYPES=':(task-definition|service|cluster|natgateway|vpc-endpoint)/'
flat_tags() {
  tr '\t' '\n' | { grep -v ':instance/' || true; } | { grep -Ev "$COVERED_TYPES" || true; } | tr '\n' ' '
}

report "tagged Project=IncidentTriage" "$(aws resourcegroupstaggingapi get-resources --region "$REGION" \
  --tag-filters Key=Project,Values=IncidentTriage \
  --query "ResourceTagMappingList[].ResourceARN" --output text 2>/dev/null | flat_tags)"
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
report "ECS clusters" "$(aws ecs list-clusters --region "$REGION" \
  --query "clusterArns[?contains(@, 'incident-triage')]" --output text 2>/dev/null | flat)"
report "ECS services (ACTIVE)" "$(aws ecs list-clusters --region "$REGION" \
  --query "clusterArns[?contains(@, 'incident-triage')]" --output text 2>/dev/null | \
  tr '\t' '\n' | while read -r arn; do
    [ -n "$arn" ] || continue
    aws ecs list-services --cluster "$arn" --region "$REGION" \
      --query 'serviceArns' --output text 2>/dev/null
  done | tr '\t' '\n' | grep -v '^$' | tr '\n' ' ' || true)"
report "ECS task definitions (ACTIVE)" "$(aws ecs list-task-definition-families --region "$REGION" \
  --status ACTIVE --query "families[?contains(@, 'incident-triage')]" --output text 2>/dev/null | flat)"
report "VPC endpoints" "$(aws ec2 describe-vpc-endpoints --region "$REGION" \
  --query "VpcEndpoints[?contains(Tags[?Key=='Project'].Value | [0], 'IncidentTriage')].VpcEndpointId" \
  --output text 2>/dev/null | flat)"
report "ECR repositories" "$(aws ecr describe-repositories --region "$REGION" \
  --query "repositories[?repositoryName=='incident-triage'].repositoryName" --output text 2>/dev/null | flat)"
report "SSM parameters (out-of-band)" "$(aws ssm describe-parameters --region "$REGION" \
  --parameter-filters "Key=Name,Option=BeginsWith,Values=$SSM_PREFIX" \
  --query "Parameters[].Name" --output text 2>/dev/null | flat)"
report "IAM deploy roles" "$(aws iam list-roles \
  --query "Roles[?contains(RoleName, 'incident-triage')].RoleName" --output text 2>/dev/null | flat)"

echo
echo "== awareness: region-wide inventory (informational, never fails the run) =="
printf '   %-30s %s\n' "all NAT gateways" "$(aws ec2 describe-nat-gateways --region "$REGION" \
  --filter Name=state,Values=available,pending --query "NatGateways[].NatGatewayId" --output text 2>/dev/null)"
printf '   %-30s %s\n' "all load balancers" "$(aws elbv2 describe-load-balancers --region "$REGION" \
  --query "LoadBalancers[].LoadBalancerName" --output text 2>/dev/null)"
printf '   %-30s %s\n' "all running instances" "$(aws ec2 describe-instances --region "$REGION" \
  --filters Name=instance-state-name,Values=running --query "Reservations[].Instances[].InstanceId" --output text 2>/dev/null)"
printf '   %-30s %s\n' "all ECR repositories" "$(aws ecr describe-repositories --region "$REGION" \
  --query "repositories[].repositoryName" --output text 2>/dev/null)"
printf '   %-30s %s\n' "INACTIVE task defs (free)" "$(aws ecs list-task-definition-families --region "$REGION" \
  --status INACTIVE --query "families[?contains(@, 'incident-triage')]" --output text 2>/dev/null)"
printf '   %-30s %s\n' "CDKToolkit stack" "$(aws cloudformation describe-stacks --region "$REGION" \
  --stack-name CDKToolkit --query "Stacks[].StackStatus" --output text 2>/dev/null || echo "not deployed")"
printf '   %-30s %s\n' "bootstrap bucket" "$(aws s3api list-buckets \
  --query "Buckets[?contains(Name, 'cdk-hnb659fds-assets')].Name" --output text 2>/dev/null)"

echo
if [ "$MODE" = "check" ]; then
  echo "check mode: $PRESENT project-scoped group(s) currently exist."
  echo "(expected while the stacks are up; run without --check to destroy and assert emptiness)"
  exit 0
fi

if [ "$PRESENT" -gt 0 ]; then
  echo "LEAK: $PRESENT project-scoped check(s) still returned resources - inspect the ARNs/IDs above."
  exit 1
fi

echo "Clean: every project-scoped check is empty. CDKToolkit and its asset bucket are"
echo "intentionally retained for future deploys (delete them only to fully de-bootstrap)."
