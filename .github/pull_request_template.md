## What changed

<!-- One paragraph. Link the ADR or incident report if this is a design or fix. -->

## Evidence

<!-- Paste real output, not a description of it. See docs/incidents/ for the standard. -->

## Deploy verification (required for anything that changes what runs)

A deploy is not finished when CloudFormation says `UPDATE_COMPLETE` — the stack
matching the template does not mean the new code is running
([INC-0001](../docs/incidents/0001-stale-code-after-successful-deploy.md)):

- [ ] `GET /health` `version` matches `git rev-parse --short HEAD`
- [ ] `scripts/smoke_test.sh <base-url>` passes against the deployed stack
- [ ] If alarms/SNS changed: the SNS subscription is **not** `PendingConfirmation`
      and a fault test reaches `ALARM` ([INC-0002](../docs/incidents/0002-fault-detection-latency-and-unconfirmed-alert-path.md))

## Checklist

- [ ] `uv run pytest` passes (unit + CDK template assertions)
- [ ] `uv run ruff check app tests infra` and `uv run bandit -q -r app` pass
- [ ] New cdk-nag findings are acknowledged with a written justification, or fixed
- [ ] Docs updated (ADR for a decision, incident report for a failure, runbook for an operation)
- [ ] No secrets, account-specific values, or credentials added to the repo
