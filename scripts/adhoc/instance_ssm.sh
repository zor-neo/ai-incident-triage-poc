#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# scripts/adhoc/instance_ssm.sh — run a command on the Level 1 EC2 instance
#
# WHEN THIS WAS USED
#   2026-10-17, during the fault-injection drills behind INC-0002. The drill was:
#       ./instance_ssm.sh stop-service     # the fault: kill the service
#       ./instance_ssm.sh target-health    # watch unhealthy -> alarm -> SNS
#       ./instance_ssm.sh start-service    # recovery
#   Run three times while tuning the alarm (v1 30s/3x60s, v2 15s/2x60s,
#   v3 15s/1x60s) to measure mean time to detect: 6m44s, 4m48s, and a v3 figure
#   that did not survive re-checking against the alarm history (see INC-0002's
#   measurement correction — the v3 number was measured across the deploy that
#   replaced the instance; a consistent ~3min CloudWatch pipeline lag dominates).
#   It was also used to read the application log and the user-data
#   log while diagnosing INC-0001.
#
# WHAT IT PROVES
#   The instance has no SSH (KeyName: null) and no public IP — Session Manager is
#   the single operator path, and it is audited by CloudTrail. This script is that
#   path, expressed once.
#
# GOTCHAS IT ENCODES (both cost real time on 2026-10-17)
#   1. `--parameters` takes a JSON document. From PowerShell 5.1, embedding the
#      double quotes needed for {"commands":["..."]} gets mangled; the fix was to
#      write the JSON to a temp file and pass `--parameters file://C:/.../x.json`.
#      From bash (Git Bash) the inline form below works, so the file dance is not
#      needed — but do not paste this into PowerShell and expect it to survive.
#   2. A stop/start of the service does NOT restart the EC2 instance, so the
#      `/health` version stays correct after recovery — useful during a drill.
#
# HOW TO RUN
#   STACK=IncidentTriage-level1 ./instance_ssm.sh status
#   Subcommands: status | stop-service | start-service | version | logs [N]
#                | user-data | target-health | raw "<shell command>"
#
#   `raw` is the escape hatch (used by drill_ai_unavailable.sh to edit
#   /etc/incident-triage.env). SSM runs it as root, so it can do anything the
#   instance can - it is an operator tool, not a safe-by-default one.
# ---------------------------------------------------------------------------
set -euo pipefail

STACK="${STACK:-IncidentTriage-level1}"
REGION="${AWS_REGION:-us-east-1}"
UNIT="${UNIT:-incident-triage}"

instance_id() {
  if [ -n "${INSTANCE_ID:-}" ]; then
    printf '%s' "$INSTANCE_ID"
    return
  fi
  aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Project,Values=IncidentTriage" \
              "Name=instance-state-name,Values=running" \
    --query "Reservations[].Instances[].InstanceId" --output text \
    | tr '\t' '\n' | grep -v '^$' | head -n1
}

run_ssm() {
  local cmd="$1" id cid
  id="$(instance_id)"
  [ -n "$id" ] || { echo "no running IncidentTriage instance found in $REGION" >&2; exit 1; }
  echo "== $STACK / $id :: $cmd"
  cid="$(aws ssm send-command --region "$REGION" \
    --instance-ids "$id" \
    --document-name AWS-RunShellScript \
    --parameters "commands=[\"$cmd\"]" \
    --query "Command.CommandId" --output text)"
  for _ in $(seq 1 30); do
    status="$(aws ssm get-command-invocation --region "$REGION" \
      --command-id "$cid" --instance-id "$id" --query "Status" --output text 2>/dev/null || echo Pending)"
    case "$status" in
      Success|Failed|Cancelled|TimedOut) break ;;
      *) sleep 2 ;;
    esac
  done
  aws ssm get-command-invocation --region "$REGION" \
    --command-id "$cid" --instance-id "$id" \
    --query "{Status:Status,StdOut:StandardOutputContent,StdErr:StandardErrorContent}" \
    --output text
}

case "${1:-}" in
  status)        run_ssm "systemctl is-active $UNIT" ;;
  # THE FAULT: the service is stopped, the ALB target goes unhealthy, the alarm fires.
  stop-service)  run_ssm "systemctl stop $UNIT; sleep 1; systemctl is-active $UNIT || true" ;;
  # RECOVERY: the alarm clears, the target returns to healthy.
  start-service) run_ssm "systemctl start $UNIT; sleep 2; systemctl is-active $UNIT; curl -s -m 3 http://127.0.0.1:8000/health" ;;
  version)       run_ssm "curl -s -m 3 http://127.0.0.1:8000/health" ;;
  logs)          run_ssm "journalctl -u $UNIT -n ${2:-30} --no-pager" ;;
  user-data)     run_ssm "tail -n 40 /var/log/cloud-init-output.log" ;;
  raw)           shift; run_ssm "$*" ;;
  target-health)
    aws elbv2 describe-target-health --region "$REGION" \
      --target-group-arn "$(aws elbv2 describe-target-groups --region "$REGION" \
        --query "TargetGroups[?contains(TargetGroupName,'Incide')].TargetGroupArn" --output text)" \
      --query "TargetHealthDescriptions[].{Id:Target.Id,State:TargetHealth.State,Reason:TargetHealth.Reason}" \
      --output table ;;
  *)
    sed -n '2,40p' "$0"; exit 2 ;;
esac
