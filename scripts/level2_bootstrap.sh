#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# scripts/level2_bootstrap.sh - create/destroy the Level 2 out-of-band secret
#
# WHY THIS EXISTS
#   The Level 2 task definition gets INTERNAL_API_KEY from an SSM SecureString.
#   The parameter is deliberately NOT created by CDK: if it were, its plaintext
#   value would appear in the CloudFormation template, in the CDK asset, and in
#   `cdk diff` output - which is exactly the mistake the design is avoiding. So
#   the parameter is a documented prerequisite, and this script is that
#   prerequisite made repeatable instead of a paragraph in a README.
#
# ORDER OF OPERATIONS (see docs/runbooks/level2-runbook.md)
#   1. scripts/level2_bootstrap.sh                 # this script: create the secret
#   2. cdk deploy IncidentTriage-level2-registry   # the registry must exist first
#   3. docker build ... && docker push ...:$SHA    # push the image before any service
#   4. cdk deploy IncidentTriage-level2 -c imageTag=$SHA
#
# USAGE
#   scripts/level2_bootstrap.sh                 # create if missing (idempotent)
#   scripts/level2_bootstrap.sh --check         # report only, never write
#   scripts/level2_bootstrap.sh --show          # print the current value (decrypted)
#   scripts/level2_bootstrap.sh --delete        # remove the parameters (teardown)
#
# The value is never printed unless --show is given, and never written to disk.
# ---------------------------------------------------------------------------
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
PREFIX="${SSM_PREFIX:-/incident-triage/level2}"
ENVIRONMENTS=(staging prod)

MODE="create"
case "${1:-}" in
  --check) MODE="check" ;;
  --show) MODE="show" ;;
  --delete) MODE="delete" ;;
  "") MODE="create" ;;
  *) echo "usage: $0 [--check|--show|--delete]" >&2; exit 2 ;;
esac

command -v aws >/dev/null 2>&1 || { echo "aws CLI not found" >&2; exit 1; }

random_value() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    # Fallback for environments without openssl (e.g. some Windows Git Bash setups).
    PYTHON="${PYTHON:-python3}"; command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python
    "$PYTHON" -c "import secrets; print(secrets.token_hex(32))"
  fi
}

parameter_exists() {
  aws ssm get-parameter --name "$1" --region "$REGION" >/dev/null 2>&1
}

for env in "${ENVIRONMENTS[@]}"; do
  name="${PREFIX}/${env}/internal-api-key"
  case "$MODE" in
    check)
      if parameter_exists "$name"; then
        echo "present : $name"
      else
        echo "MISSING : $name  (run without --check to create)"
        exit 1
      fi
      ;;
    create)
      if parameter_exists "$name"; then
        echo "present : $name (left unchanged)"
      else
        aws ssm put-parameter \
          --name "$name" \
          --type SecureString \
          --value "$(random_value)" \
          --description "Runtime secret for the incident-triage ${env} ECS task (Level 2)" \
          --tags "Key=Project,Value=IncidentTriage" "Key=Environment,Value=level2" \
                 "Key=Component,Value=delivery" "Key=ManagedBy,Value=manual" \
          --region "$REGION" >/dev/null
        echo "created : $name"
      fi
      ;;
    show)
      if parameter_exists "$name"; then
        printf 'value   : %s = %s\n' "$name" \
          "$(aws ssm get-parameter --name "$name" --with-decryption \
               --region "$REGION" --query 'Parameter.Value' --output text)"
      else
        echo "missing : $name"; exit 1
      fi
      ;;
    delete)
      if parameter_exists "$name"; then
        aws ssm delete-parameter --name "$name" --region "$REGION"
        echo "deleted : $name"
      else
        echo "absent  : $name (nothing to delete)"
      fi
      ;;
  esac
done

if [ "$MODE" = "create" ]; then
  echo
  echo "Next: cdk deploy IncidentTriage-level2-registry, push the image, then"
  echo "      cdk deploy IncidentTriage-level2 -c imageTag=\$(git rev-parse --short HEAD)"
  echo "Read the value later with: $0 --show"
fi
