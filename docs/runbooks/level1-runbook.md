# Runbook: AI Incident & Log Triage - Level 1 (EC2)

Operational runbook for the Level 1 deployment. Pair it with
`docs/network-design.md` and the ADRs in `docs/adr/`.

- **Stack:** `IncidentTriage-level1`
- **Region:** `us-east-1`
- **Entry point:** ALB DNS name from the `ApplicationUrl` stack output
- **App runtime:** `t4g.small` AL2023 (arm64), systemd unit `incident-triage`,
  user `appuser`, port `8000`
- **Logs:** CloudWatch Logs `/aws/ec2/incident-triage`, stream = instance id
- **Alerts:** SNS topic `IncidentTriage-alerts-level1` (owner: `justin@zorneo.dev`)

---

## 1. Deploy

```bash
cd infra
uv run --no-sync cdk diff        # review what will change
uv run --no-sync cdk deploy       # creates/updates the stack
```

`cdk.json` sets `"requireApproval": "never"` because this is a single-developer
lab whose change gate is `cdk diff` in review (Level 3). For any environment
that matters, pass `--require-approval broadening` instead.

First boot takes **2-5 minutes** (measured: ~2 minutes from instance launch to a
healthy target in this session): the instance installs system packages, builds
a venv, installs the pinned lock file, then starts the service. Until it
finishes, the target is `unhealthy` - this is expected. The ALB unhealthy
alarm requires ~4-5 minutes of continuous failure before it fires to avoid
noise during provisioning.

**Application changes replace the instance** (`user_data_causes_replacement=True`,
ADR 0006). Expect a short window with one bootstrapping target: the old target is
drained, the new instance registers only after `/health` passes. If a deploy
reports success but `GET /health` still shows the old `version`, the new code was
not run - see `docs/incidents/0001-stale-code-after-successful-deploy.md`.

## 2. Verify

```bash
BASE_URL=$(aws cloudformation describe-stacks --stack-name IncidentTriage-level1 \
  --query "Stacks[0].Outputs[?OutputKey=='ApplicationUrl'].OutputValue" --output text)

curl -s "$BASE_URL/health"          # {"status":"healthy","version":"<git sha>","environment":"level1"}
scripts/smoke_test.sh "$BASE_URL"   # end-to-end ingest + redaction check
open "$BASE_URL/docs"               # FastAPI OpenAPI UI
```

**A deploy is not finished until `version` matches `git rev-parse --short HEAD`.**
`UPDATE_COMPLETE` proves the stack matches the template, not that the new code is
running. Fail the deploy if they differ:

```bash
DEPLOYED=$(curl -s "$BASE_URL/health" | python -c "import json,sys; print(json.load(sys.stdin)['version'])")
test "$DEPLOYED" = "$(git rev-parse --short HEAD)" || { echo "STALE DEPLOY: $DEPLOYED != HEAD"; exit 1; }
```

## 3. Troubleshoot a failing target

Work outside-in; each step narrows the fault domain.

1. **Is the target unhealthy or the whole service down?**
   ```bash
   TG=$(aws elbv2 describe-target-groups --load-balancer-arn "$ALB_ARN" \
     --query "TargetGroups[0].TargetGroupArn" --output text)
   aws elbv2 describe-target-health --target-group-arn "$TG"
   ```
   - `Target.Timeout` / `Target.FailedHealthChecks` → the app is not answering
     on `:8000/health`.
   - `Target.NotInUse` → the instance is not registered (user data never
     finished, or the instance was replaced).

2. **Get on the box without SSH keys (Session Manager):**
   ```bash
   aws ssm start-session --target <InstanceId>   # id is in the stack outputs
   ```
   If this fails, the instance role is missing SSM permissions or the instance
   has no egress (NAT) - check the NAT gateway and route table first.

3. **On the instance:**
   ```bash
   systemctl status incident-triage --no-pager
   journalctl -u incident-triage -n 50 --no-pager
   tail -n 50 /var/log/incident-triage/app.log
   sudo -u appuser /opt/incident-triage/.venv/bin/python -c "import app.main"
   ```

4. **Application errors → CloudWatch Logs Insights** (fields are JSON, so query
   them directly):
   ```
   fields @timestamp, level, request_id, incident_id, status, error
   | filter level = "error"
   | sort @timestamp desc
   ```

5. **AWS API errors (AccessDenied on Bedrock or DynamoDB)** - confirm the exact
   call and identity in CloudTrail:
   ```bash
   aws cloudtrail lookup-events \
     --lookup-attributes AttributeKey=EventName,AttributeValue=InvokeModel \
     --max-results 10
   ```

### Common causes seen at this level

| Symptom | Likely cause | Fix |
| :--- | :--- | :--- |
| Target unhealthy, service `active` | App listening on the wrong port/IP | Confirm `--port 8000 --host 0.0.0.0` in the unit |
| `202 AI_UNAVAILABLE` for every request | Bedrock model id wrong, or no model access in the region | Check `BEDROCK_MODEL_ID`, and `aws bedrock get-foundation-model-availability` |
| `AccessDeniedException` in app log | Instance role missing `bedrock:InvokeModel` for the ARN | Confirm the model ARN in the role's `InvokeTriageModel` statement |
| Session Manager won't connect | NAT/egress broken or SSM perms missing | Check NAT gateway state and the `SessionManagerAccess` statement |
| No logs in CloudWatch | Agent not running or wrong log path | `systemctl status amazon-cloudwatch-agent`, check `/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json` |
| Deploy green but `/health` `version` is old | User data changed without replacing the instance | Confirm `user_data_causes_replacement=True` on `AppInstance` (ADR 0006) |

## 4. Roll back

Deploys are code-as-config, so a rollback is a redeploy of the previous state.

```bash
cd infra
git log --oneline -5               # find the last good commit
git checkout <good-sha> -- .       # or: git revert <bad-sha>
uv run --no-sync cdk diff
uv run --no-sync cdk deploy
```

CloudFormation also supports `cdk deploy --rollback` behavior on failed stack
updates, but because the instance bootstraps from a versioned S3 asset, the
authoritative rollback is "deploy the last good git SHA". Level 3 replaces this
manual step with ECS deployment alarms + automatic rollback.

## 5. Recovery lab (terminate and rebuild from code)

1. Terminate the instance out of band (EC2 console, or
   `aws ec2 terminate-instances --instance-ids <id>`).
2. `uv run --no-sync cdk deploy` **does not** recreate it if CloudFormation still
   believes it exists. Application changes replace the instance (ADR 0006), but a
   pure out-of-band termination does not change the template, so nothing reconciles
   it. Force reconciliation by replacing the resource:
   ```bash
   cdk deploy --force                     # or delete just the instance resource
   ```
   Practically, the reliable exercise is `cdk destroy && cdk deploy` and
   measuring how long the service takes to serve `/health` again - that is your
   **recovery time** for the incident report.
3. Record MTTD (first alarm → noticed) and MTTR (start → healthy) for the
   post-incident review.

## 6. Cost control and teardown

While up, the stack costs roughly **$0.09/hour** (NAT gateway, ALB, `t4g.small`,
plus a few cents of CloudTrail/S3/Bedrock). An idle month would be ~$65; a
one-day lab is **~$2**. A $10 monthly budget with 80% actual / 100% forecasted
alerts is created by the stack.

```bash
scripts/cleanup.sh                # cdk destroy + orphan check
scripts/cleanup.sh --check        # orphan check only - destroys nothing
```

After a real teardown, every project-scoped section must print nothing (the script
exits non-zero if any does). `--check` run while the stack is up is the way to
prove the checks work at all: each section should list its live resource, which is
exactly the evidence that the check is wired to the right API.

`CDKToolkit` and its asset bucket are intentionally retained so future deploys are
fast; delete them only to fully de-bootstrap the account/region. Terminated EC2
instances stay visible to the tagging API for roughly an hour after a replacement,
which is why the tag check ignores instance ARNs and instance state is asserted
separately.

## 7. Game day checklist

Run these from memory, in order, without looking at the runbook. Timings below are
the ones measured on 2026-10-17 (`docs/incidents/0002-*`) - treat them as
expectations to verify, not as fixed constants.

**Prerequisite: the alert path must be confirmed before any of this means anything.**
```bash
aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:us-east-1:<account>:IncidentTriage-alerts-level1 \
  --query "Subscriptions[].{EP:Endpoint,ARN:SubscriptionArn}"
```
`SubscriptionArn` must be a real ARN, **not `PendingConfirmation`**. If it is
pending, click the AWS confirmation email first - otherwise every alarm is silent.

Each step below is scripted in [`scripts/adhoc/`](../../scripts/adhoc/README.md) so
it can be re-run rather than retyped: `instance_ssm.sh` drives step 1, and
`drill_ai_unavailable.sh` automates step 2 (it deploys twice, so it is guarded
behind `--confirm-degradation`).

1. Break the health check (`systemctl stop incident-triage` via SSM RunCommand),
   confirm the ALB target goes `unhealthy` in **~60s**, the
   `IncidentTriage-level1-alb-unhealthy-targets` alarm reaches `ALARM` in
   **~1.5 min**, and an email arrives. Restore and confirm recovery in **~45s**.
   Measured before tuning: 78s to `unhealthy`, 6m44s to `ALARM`, 0 alerts delivered.
2. Force `AI_UNAVAILABLE` (point `BEDROCK_MODEL_ID` at a non-existent model,
   restart) and confirm the API still returns `202` and stores the incident.
   Measured 2026-10-17: `202 AI_UNAVAILABLE` in 54ms with the incident persisted
   and the raw `sanitized_log` intact; logs show `bedrock_invoke_failed`
   (`ValidationException`, `reached max retries: 0`) and `triage_fallback`
   (`reason=client_error`), and the metric `TriageFallbackByReason{Reason=client_error}`
   appears in the `IncidentTriage` namespace. Reverting the env var and restarting
   restores `201 TRIAGED` (~500ms).
3. Deny one narrowly scoped permission (remove `dynamodb:GetItem`) and confirm
   the failure is the expected one, not a crash.
4. Destroy the stack and verify zero orphans with `scripts/cleanup.sh`.
