#!/usr/bin/env bash
# FinOps teardown: destroy the Level 1 stack and verify nothing was orphaned.
#
# Usage: scripts/cleanup.sh [stack-name]
set -euo pipefail

STACK_NAME="${1:-${STACK_NAME:-IncidentTriage-level1}}"
REGION="${AWS_REGION:-us-east-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== destroying $STACK_NAME in $REGION =="
( cd "$SCRIPT_DIR/../infra" && uv run --no-sync cdk destroy "$STACK_NAME" --force )

echo
echo "== orphan check: resources still tagged Project=IncidentTriage =="
aws resourcegroupstaggingapi get-resources \
  --region "$REGION" \
  --tag-filters Key=Project,Values=IncidentTriage \
  --query "ResourceTagMappingList[].ResourceARN" --output text

echo "== orphan check: NAT gateways =="
aws ec2 describe-nat-gateways --region "$REGION" \
  --filter "Name=state,Values=available,pending" \
  --query "NatGateways[].NatGatewayId" --output text

echo "== orphan check: load balancers =="
aws elbv2 describe-load-balancers --region "$REGION" \
  --query "LoadBalancers[].LoadBalancerName" --output text

echo "== orphan check: DynamoDB tables =="
aws dynamodb list-tables --region "$REGION" \
  --query "TableNames[?starts_with(@, 'IncidentTriage')]" --output text

echo
echo "Empty output above for every section means the teardown was clean."
