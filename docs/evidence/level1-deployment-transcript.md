# Level 1 deployment transcript (2026-10-17)

Verbatim (trimmed) record of the first real deployment of this stack to a live AWS
account, including the two failures it surfaced. Nothing here is reconstructed
from memory - every block was pasted from the session that produced it. Command
outputs are truncated with `...` where a resource list was long.

- **Account / region:** `685134815483` / `us-east-1`
- **Stack:** `IncidentTriage-level1`
- **Operator:** repo owner (solo), no SSH - Session Manager and SSM RunCommand only
- **Tooling:** uv 0.8.15, CDK CLI 2.1145.0, aws-cdk-lib 2.273.0, AWS CLI 2.34.30,
  Node 22.17.1, Python 3.13.11 local (target runtime 3.12 from the lock file)

## 1. Bootstrap (one-time per account/region)

```
$ cdk bootstrap aws://685134815483/us-east-1
CDKToolkit | 12/12 | CREATE_COMPLETE | AWS::CloudFormation::Stack | CDKToolkit
✅  Environment aws://685134815483/us-east-1 bootstrapped.
```

## 2. `cdk diff` before the first deploy

```
$ cdk diff
[+] AWS::EC2::Instance AppInstance ...
[+] AWS::EC2::LaunchTemplate AppInstance/LaunchTemplate ...
[+] AWS::ElasticLoadBalancingV2::LoadBalancer Alb ...
[+] AWS::DynamoDB::Table IncidentTable ...
[+] AWS::CloudTrail::Trail Trail ...
...
Outputs
[+] Output ApplicationUrl: {"Value":{"Fn::Join":["",["http://",{"Fn::GetAtt":["Alb16C2F182","DNSName"]}]]}}
✨  Number of stacks with differences: 1
```

## 3. First deploy

```
$ cdk deploy
IncidentTriage-level1 | 55/55 | CREATE_COMPLETE | AWS::CloudFormation::Stack | IncidentTriage-level1
✅  IncidentTriage-level1
✨  Deployment time: 204.83s
Outputs:
IncidentTriage-level1.ApplicationUrl = http://Incide-Alb16-42G9qBLXchCg-1665380019.us-east-1.elb.amazonaws.com
IncidentTriage-level1.InstanceId = i-06425bd3f7445dcd6
IncidentTriage-level1.TableName = IncidentTriage-level1
IncidentTriage-level1.LogGroupName = /aws/ec2/incident-triage
IncidentTriage-level1.ModelId = amazon.nova-micro-v1:0
IncidentTriage-level1.SsmStartSession = aws ssm start-session --target i-06425bd3f7445dcd6
✨  Total time: 263.28s
```

First boot: the target was `unhealthy` for ~2 minutes while user data installed the
lock file; `/health` answered on attempt 9 of the polling loop:

```
attempt 8 : 200 {"status":"healthy","version":"76ad3dd","environment":"level1"}
```

## 4. Smoke test against the live stack

```
$ scripts/smoke_test.sh http://Incide-Alb16-42G9qBLXchCg-1665380019.us-east-1.elb.amazonaws.com
== smoke test against http://Incide-...elb.amazonaws.com ==
-- GET /health
   healthy, version=76ad3dd
-- POST /incidents
   HTTP 201 -> incident_id=inc_01M555979FNQ0YKZTMXAT3WDDR status=TRIAGED
   ok: AWS key redacted
-- GET /incidents/inc_01M555979FNQ0YKZTMXAT3WDDR
   ok: stored status matches
== smoke test passed ==
```

## 5. Real triage output (Nova Micro, live Bedrock)

Request contained an AWS access key, an email, a private IP, a bearer token, and a
Secrets Manager ARN. Response (trimmed):

```json
{
  "incident_id": "inc_01M5559VV2ZK39PDWW69Y6B6QW",
  "request_id": "evidence-002",
  "status": "TRIAGED",
  "sanitized_log": "botocore... GetSecretValue operation for arn:aws:secretsmanager:us-east-1:685134815483:secret:[REDACTED_SECRET] (Access key: [REDACTED_AWS_KEY]). Contact [REDACTED_EMAIL], backend [REDACTED_IP]:5432, Authorization: Bearer [REDACTED_SECRET]",
  "ai_triage": {
    "summary": "The payment-worker service failed to retrieve a secret from AWS Secrets Manager due to an AccessDeniedException.",
    "probable_cause": "The IAM credentials used by the payment-worker service do not have sufficient permissions to access the specified secret.",
    "first_action": "Check the IAM policy attached to the service's IAM role for the GetSecretValue permission on the specified secret."
  },
  "metrics": {"ai_latency_ms": 530, "input_tokens": 234, "output_tokens": 91}
}
```

This output exposed the first defect: the redactor consumed the secret **resource
path** (`secret:prod/payment/db-pass`), making the ARN useless to the engineer
while the triage advice pointed at it.

## 6. Failure: a green deploy that shipped nothing

```
$ curl -s $BASE_URL/health
{"status":"healthy","version":"76ad3dd","environment":"level1"}   # expected 1d06863

$ aws cloudformation describe-stack-events --stack-name IncidentTriage-level1
| AppInstance13C82D50 | UPDATE_IN_PROGRESS | 2026-10-17T14:50:16Z |
| AppInstance13C82D50 | UPDATE_COMPLETE    | 2026-10-17T14:50:50Z |
| IncidentTriage-level1 | UPDATE_COMPLETE  | 2026-10-17T14:50:52Z |
```

CloudFormation did what a user-data change means (restart, `Update requires: Some
interruptions`) and cloud-init ran the script once per instance, not once per boot.
The fix and its proof are in
[INC-0001](../incidents/0001-stale-code-after-successful-deploy.md); the change set
before deploying showed a replacement, not an update:

```
$ cdk diff -c gitSha=probe-replacement-check
[-] AWS::EC2::Instance AppInstance AppInstance13C82D50 destroy
[+] AWS::EC2::Instance AppInstance AppInstance13C82D507c2b21d2103f83c1
```

## 7. Verified fix (deploy replaces the instance, code actually ships)

```
$ cdk deploy
IncidentTriage-level1.InstanceId = i-086504b397af2e077     # replaced, new id
✨  Deployment time: 143.09s

$ curl -s $BASE_URL/health
{"status":"healthy","version":"5e04ba0","environment":"level1"}   # new commit, live

$ # POST with the same AccessDenied log
arn_preserved = True
key_redacted  = True
ai_first_action: Check IAM permissions for the service role accessing the secret.
```

## 8. Metrics (custom namespace `IncidentTriage`)

```
$ aws cloudwatch get-metric-statistics --namespace IncidentTriage \
    --metric-name TriageSuccess --dimensions Name=Environment,Value=level1 ...
Timestamp 21:59+07:00  Sum: 1.0     # post-fix verification request
Timestamp 21:47+07:00  Sum: 3.0     # smoke + 2 evidence requests

$ aws cloudwatch get-metric-statistics ... --metric-name BedrockLatencyMs
21:55+07:00  Average: 445.0         # matches the reported ai_latency_ms
21:45+07:00  Average: 1153.3
```

Note: querying without `--dimensions` returned an empty result set even though the
metric exists; the dimensions must be supplied.

## 9. Structured logs (CloudWatch Logs Insights fields are real JSON keys)

```
{"service": "smoke-test", "severity": "ERROR", "redactions": {"aws_access_key_id": 1, "email": 1, "ipv4": 1}, "event": "incident_persisted", "request_id": "smoke-test-run", "git_sha": "76ad3dd", "incident_id": "inc_01M555979FNQ0YKZTMXAT3WDDR", "environment": "level1", "level": "info", "timestamp": "..."}
{"latency_ms": 2489, "input_tokens": 161, "output_tokens": 53, "event": "bedrock_triaged", "request_id": "smoke-test-run", ...}
{"method": "POST", "path": "/incidents", "status": 201, "duration_ms": 2605.36, "event": "request", "request_id": "smoke-test-run", ...}
```

## 10. Fault injection: how long does detection actually take?

Environment broken deliberately with SSM RunCommand (no SSH keys exist):

```bash
aws ssm send-command --document-name AWS-RunShellScript \
  --instance-ids <id> --parameters 'commands=["systemctl stop incident-triage"]'
```

| Iteration (config) | Break | Target `unhealthy` | Alarm `ALARM` | MTTD |
| :--- | :--- | :--- | :--- | :--- |
| 30s health check, 3x60s alarm | 15:03:57 | ~15:05:15 | 15:10:41 | 6m44s |
| 15s health check, 2x60s alarm | 15:22:44 | 15:23:42 | 15:27:39 | 4m55s |
| 15s health check, 1x60s alarm | 15:35:48 | 15:36:47 | 15:37:13 | 1m25s |

> **Correction (2026-10-18).** The v2 and v3 rows above do not survive re-checking
> against `describe-alarm-history`. v2 was **4m48s** (transition 15:27:32, not
> 15:27:39), and the v3 transition logged here (15:37:13) actually cites a metric
> datapoint from **before** the injection — a symptom of measuring across the
> `cdk deploy` that replaced the instance. The honest v3 figure is **~3 minutes**,
> dominated by a consistent ~3-minute CloudWatch pipeline lag. See
> [INC-0002](../incidents/0002-fault-detection-latency-and-unconfirmed-alert-path.md)
> (measurement correction) and the
> [access-log forensics](level1-access-log-forensics.md). The table is left as
> written; this note is the correction.

Recovery after `systemctl start`: 43s to `healthy` + alarm `OK` (was ~3m45s) —
same caveat: the "43s" used the mis-attributed v3 transition (see above).
Alarm transition, taken from `describe-alarm-history`:

```
Alarm updated from OK to ALARM              2026-10-17T15:10:41.195Z
```

**Zero alerts were delivered all session**: the SNS subscription is
`PendingConfirmation`.

```
$ aws sns list-subscriptions-by-topic --topic-arn arn:...:IncidentTriage-alerts-level1
[{"Proto": "email", "EP": "justin@zorneo.dev", "ARN": "PendingConfirmation"}]
```

See [INC-0002](../incidents/0002-fault-detection-latency-and-unconfirmed-alert-path.md).

> **Postscript (later the same day).** The owner confirmed the subscription:
> `list-subscriptions-by-topic` now yields a real `SubscriptionArn`
> (`…:3803e2c5-…`), and an `aws sns publish` test (MessageId `40efef95-…`) was
> sent. Alarm and AWS Budget notifications now have somewhere to land, and
> `scripts/adhoc/verify_alert_path.sh` fails if any subscription is ever pending
> again. The transcript above is unchanged: at the time, nothing was delivered.

## 11. Graceful degradation, verified in production

```
$ # BEDROCK_MODEL_ID=amazon.nova-does-not-exist-v1:0, restart
$ curl -s -X POST $BASE_URL/incidents ...
HTTP 202 status=AI_UNAVAILABLE ai_triage= message=AI triage unavailable; inspect raw sanitized_log and service CloudWatch logs directly.

{"reason": "client_error", "error_type": "ValidationException", "latency_ms": 54, "event": "bedrock_invoke_failed", "request_id": "degraded-001", ...}
{"reason": "client_error", "detail": "An error occurred (ValidationException) when calling the InvokeModel operation (reached max retries: 0): The provided model identifier is invalid.", "event": "triage_fallback", ...}

$ aws cloudwatch get-metric-statistics --namespace IncidentTriage --metric-name TriageFallback ...
22:40+07:00  Sum: 1.0
$ aws cloudwatch list-metrics --namespace IncidentTriage   # TriageFallbackByReason {Reason: client_error}

$ # reverted
HTTP 201 status=TRIAGED latency=504ms
```

The incident was persisted with its raw `sanitized_log` and an actionable message,
no 5xx, and the failure is visible as a metric and as structured logs - including
`reached max retries: 0`, confirming the single-attempt Bedrock configuration.

## 12. Final state of this session

```
$ curl -s $BASE_URL/health
{"status":"healthy","version":"5d3733a","environment":"level1"}
```

- Instance `i-003f1651c312481d4`, target group healthy, all three alarms `OK`.
- Git: 13 commits, working tree clean.
- Left running deliberately (owner capture of screenshots/video still to do).

## Known gaps carried out of this session

1. **The alert path is unproven end to end.** SNS email subscription is
   `PendingConfirmation`; until it is confirmed, every alarm here is decorative.
2. **No screenshot/video evidence** captured by the operator yet.
3. **Teardown not yet verified** for this stack; `scripts/cleanup.sh` must be run
   and must report zero orphans (note: `CWAlarmTest`, `AWSQuickSetupCWAgentPermissionsStack`,
   and a failed `Infra-ECS-Cluster-*` stack pre-date this project and belong to
   earlier labs, so the orphan check is scoped to `IncidentTriage-*` / `CDKToolkit`).
