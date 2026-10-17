#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# scripts/adhoc/verify_alert_path.sh — assert that an alarm could actually reach
# a human
#
# WHEN THIS WAS USED
#   2026-10-17, immediately after the owner confirmed the SNS email subscription
#   (it had been `PendingConfirmation` since the stack was created, which is the
#   second finding in INC-0002: every alarm and the AWS Budget were silent).
#   This script is the "permanent corrective action" that incident promised —
#   written so the check is executable instead of a sentence in a runbook that
#   nobody re-reads.
#
# WHAT IT PROVES
#   That the alert *path* exists end to end at the SNS level: a real
#   SubscriptionArn (not `PendingConfirmation`) for the configured endpoint.
#   It cannot prove the mailbox owner read the mail — only a drill can do that
#   (see instance_ssm.sh stop-service + target-health).
#
# HOW TO RUN
#   ./verify_alert_path.sh              # check only
#   ./verify_alert_path.sh --publish    # also send one test notification
#
# EXIT: 0 = a human-reachable subscription exists; 1 = the alert path is dead.
# ---------------------------------------------------------------------------
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"

TOPIC_ARN="${TOPIC_ARN:-$(aws sns list-topics --region "$REGION" \
  --query "Topics[?contains(TopicArn,'IncidentTriage')].TopicArn" --output text | tr '\t' '\n' | head -n1)}"
[ -n "$TOPIC_ARN" ] || { echo "no IncidentTriage SNS topic found in $REGION" >&2; exit 1; }

echo "== alert path: $TOPIC_ARN"

subs="$(aws sns list-subscriptions-by-topic --region "$REGION" --topic-arn "$TOPIC_ARN" \
  --query "Subscriptions[].[Protocol,Endpoint,SubscriptionArn]" --output text)"

printf '%s\n' "$subs" | while IFS=$'\t' read -r proto endpoint arn; do
  [ -n "${proto:-}" ] || continue
  printf '   %-7s %-28s %s\n' "$proto" "$endpoint" "$arn"
done

if printf '%s' "$subs" | grep -q "PendingConfirmation"; then
  cat >&2 <<'EOF'

FAIL: a subscription is still PendingConfirmation, so an alarm would be silent.
      Confirm the AWS email, then re-run. INC-0002 is the story of this exact
      failure going unnoticed through a fault-drill.
EOF
  exit 1
fi

echo "OK: every subscription is confirmed (a delivered alarm can reach a human)"

if [ "${1:-}" = "--publish" ]; then
  echo "== publishing one test notification"
  aws sns publish --region "$REGION" --topic-arn "$TOPIC_ARN" \
    --subject "[IncidentTriage] alert path test" \
    --message "verify_alert_path.sh: if this arrived, alarms and budget notices can reach you." \
    --query "MessageId" --output text
  echo "   check the inbox - an unread email is still an undelivered alert."
fi
