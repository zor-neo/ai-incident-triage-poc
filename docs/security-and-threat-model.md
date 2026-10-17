# Security Model & Threat Model — Level 1

**Scope:** the Level 1 stack (`IncidentTriage-level1`, `us-east-1`, account
`685134815483`) only. Everything here was read back from the **deployed** stack
(`aws cloudformation get-template`) or from live APIs, not from the source code —
so each claim is evidence, and any drift is visible.

Related: [network design](network-design.md) · [ADRs](adr) · [runbook](runbooks/level1-runbook.md) · [PRD §7](../product_requirements_document.md)

## 1. Trust boundaries and data flow

```
Internet ──▶ ALB (public subnets, :80, SG 0.0.0.0/0)
                 └─▶ EC2 instance (private subnets, no public IP, :8000 from ALB SG only)
                          ├─▶ DynamoDB  (PutItem/GetItem/UpdateItem, one table)
                          ├─▶ Bedrock   (InvokeModel, one model ARN)
                          ├─▶ S3 asset  (read-only, bootstrap bucket)
                          ├─▶ CloudWatch logs + custom metrics (namespace IncidentTriage)
                          └─▶ SSM Session Manager (operator access, no SSH)
Operator ──▶ SSM (no inbound port 22); deployment via cdk deploy from a workstation
```

Two boundaries matter: **internet → ALB** (unauthenticated, cleartext HTTP — see
RISK-1/2) and **instance → AWS APIs** (all credentials are short-lived STS
credentials from the instance profile; there are no long-lived keys anywhere).

Request handling order is deliberate (FR-02/FR-03): the payload is **sanitized
before it is persisted and before it is sent to the model**, and the row is written
as `PENDING_TRIAGE` *before* the Bedrock call, so a model outage can never lose an
incident. `tests/unit/test_main.py::test_incident_is_persisted_as_pending_before_the_ai_call`
enforces that ordering.

## 2. Identities in the stack

| Identity | Type | Trusted principal | Purpose |
| :--- | :--- | :--- | :--- |
| `InstanceRole3CCE2F1D` | `AWS::IAM::Role` | `ec2.amazonaws.com` (only) | the application: DynamoDB, Bedrock, logs, metrics, Session Manager |
| `…InstanceProfile…` | `AWS::IAM::InstanceProfile` | — | delivers role credentials to the instance via IMDSv2 |
| `CustomS3AutoDeleteObjects…Role` | `AWS::IAM::Role` | `lambda.amazonaws.com` | CDK's bucket-emptying helper on stack delete, scoped to the two buckets it owns |
| IAM user `admin` | out-of-band | — | human `cdk deploy`; **not** part of the stack and **not** used by the app |

The role's trust policy is exactly (live template):

```json
{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
  "Principal": {"Service": "ec2.amazonaws.com"}}]}
```

No application AWS keys exist in the repo (asserted by
`tests/infra/test_level1_stack.py` and by Bandit in CI), and none are needed: the
app calls `boto3` with the instance profile.

## 3. Instance role permissions, verbatim

| Sid / statement | Actions | Resource | Why |
| :--- | :--- | :--- | :--- |
| *(asset read)* | `s3:GetObject*`, `s3:GetBucket*`, `s3:List*` | CDK bootstrap asset bucket | fetch the app artifact during boot (see RISK-3) |
| *(table access)* | `dynamodb:PutItem`, `GetItem`, `UpdateItem` | `IncidentTable8CE1664B.Arn` | persist/update incidents — note **no** `DeleteItem`, `Scan`, `Query` |
| `InvokeTriageModel` | `bedrock:InvokeModel` | `…:foundation-model/amazon.nova-micro-v1:0` and `…:<account>:inference-profile/*` | one model, not `bedrock:*` |
| `PublishCustomMetrics` | `cloudwatch:PutMetricData` | `*` **conditioned** on `cloudwatch:namespace = IncidentTriage` | bookkeeping metrics only; cannot pollute other namespaces |
| `WriteApplicationLogs` | `logs:CreateLogStream`, `PutLogEvents`, `DescribeLogStreams` | the app log group ARN and `ARN:*` | app logging |
| `CloudWatchAgentDiscovery` | `logs:DescribeLogGroups` | `*` | forced: this is a list action with **no** resource-level ARN in IAM; it grants no access to log *data* (`logs:GetLogEvents` is not granted) |
| `SessionManagerAccess` | `ssm:UpdateInstanceInformation`, `ssmmessages:*`, `ec2messages:*` | `*` | forced: documented as not supporting resource-level permissions. This is the *only* operator path — no SSH, no key pair on the instance |

**Deliberately absent:** `secretsmanager:*`, `kms:*`, `sts:AssumeRole`, `s3:PutObject`,
`s3:DeleteObject`, `dynamodb:DeleteItem`, `dynamodb:Scan`, `bedrock:InvokeModelWithResponseStream`,
`ec2:*`, `iam:*`. The instance cannot mint new credentials, decrypt arbitrary KMS
keys, read secrets, write to S3, or enumerate the table.

## 4. Protection controls, as verified live

| Control | Verified evidence (2026-10-17) |
| :--- | :--- |
| IMDSv2 required | `describe-instances` → `MetadataOptions: {HttpTokens: "required", HttpPutResponseHopLimit: 2, State: "applied"}` |
| App reachable only via the ALB | app SG ingress `8000` from `sg-0589d9ff1fe50cd89` (ALB SG) only; ALB SG ingress `80` from `0.0.0.0/0` |
| No SSH | instance `KeyName: null`; operator access via Session Manager, which is audited by CloudTrail |
| Private workload subnet | instance has no public IP; egress via 1 NAT gateway + S3/DynamoDB gateway endpoints ([network design](network-design.md)) |
| EBS encryption | `describe-volumes` → `Encrypted: true`, `KmsKeyId: …:key/daefd4a7-…`, 8 GB `gp3`; explicit in the template (account default encryption is **off**, so this is a stack control, not an account accident) |
| DynamoDB at rest | `SSESpecification.SSEEnabled: true` |
| S3 hardening | both buckets: `BlockPublicAcls/IgnorePublicAcls/BlockPublicPolicy/RestrictPublicBuckets: true`; both bucket policies deny `s3:*` when `aws:SecureTransport = false` (TLS-only); CloudTrail bucket grants only `cloudtrail.amazonaws.com`; ALB log bucket grants only the ELB service account for `us-east-1` (`127311923021`) |
| Audit trail | CloudTrail `IsLogging: true`, `EnableLogFileValidation: true`, `IncludeGlobalServiceEvents: true`, management events `All`; ALB access logs enabled to S3 |
| Cost allocation / FinOps tags | `Project`, `Environment`, `Component`, `Owner`, `ManagedBy` present on the instance role, table, ALB, trail and more |
| Guardrails in CI | `cdk-nag` (`AwsSolutionsChecks`) fails the build on any unacknowledged finding; each suppression is justified inline in the stack |

## 5. Threat model (STRIDE-lite, Level 1 scope)

| ID | Scenario | Control now | Residual risk | Next level |
| :--- | :--- | :--- | :--- | :--- |
| RISK-1 | Anyone on the internet uses the API (no authN) | none — accepted for an ephemeral lab; data is sanitized and the app has no side effects beyond its own table | API reachable while the stack is up — and **is being probed in fact, not just in theory** (RISK-11) | HTTPS + auth (Cognito/ALB auth) or internal-only ALB |
| RISK-2 | Log content read/modified in transit (HTTP :80) | payload sanitized before storage; no secrets in the DB | cleartext hop to the ALB | ACM cert + HTTPS listener, HSTS |
| RISK-3 | Instance reads the shared CDK bootstrap asset bucket | read-only (`GetObject*`/`List*`), no write/delete | bucket holds assets for every CDK app in this account/region | per-app artifact path, ECR image + digest pinning, IAM Access Analyzer review |
| RISK-4 | Credential theft from the instance | no long-lived keys; IMDSv2 required; role scoped to one table/one model; no `sts:AssumeRole` | an operator with shell could act as the role | short session policies, ECS task role (L2), anomaly detection |
| RISK-5 | Secret/PII leaves via logs or the model | regex sanitizer runs before persist and before invoke; redaction counts are logged; tested patterns: AWS keys, secret keys, Bearer tokens, emails, IPv4 | regex blind spots — an unlisted format is not redacted | add entropy-based detection; unit-test corpus grows with each real payload |
| RISK-6 | Malicious/compromised dependency at boot | pinned, lock-file-driven install from PyPI; no code from arbitrary URLs | upstream compromise of a pinned version | ECR image scanning on push, digest pinning, SBOM |
| RISK-7 | Lateral movement from the instance | single ingress path (ALB SG → :8000); no SSH; no `ec2:*`/`iam:*` in the role | compromised app can call its three permitted API groups | VPC endpoints for DynamoDB/Bedrock, GuardDuty |
| RISK-8 | Missing/rusted audit data | CloudTrail with file validation + ALB access logs + JSON app logs with `request_id` | trail is single-region; CloudTrail bucket has **no** server access logging, SSE-S3 not KMS | multi-region trail, object-level access logs, KMS CMK + bucket key |
| RISK-9 | The alert path is dead, so faults are invisible | CloudWatch alarms wired to SNS | **this actually happened:** the email subscription sat in `PendingConfirmation`, so zero alerts were delivered through a real fault injection ([INC-0002](incidents/0002-fault-detection-latency-and-unconfirmed-alert-path.md)). Confirmed 2026-10-17; `scripts/adhoc/verify_alert_path.sh` now fails while any subscription is pending | no dead-man's-switch alarm yet, and email is still a human channel with no acknowledgement | Level 2/3: SNS→Lambda→Slack/PagerDuty (a machine subscription cannot be pending) plus an alarm on the alerting path itself |
| RISK-10 | Silent no-op deploy (change appears successful, ships nothing) | instance replacement forced on user-data change; `/health` version check is a mandatory deploy step | any future change that does not alter user data could still no-op | immutable artifact + explicit version assertion in CI (L2) |
| RISK-11 | Opportunistic internet scanning reaches the application | the app serves only the routes it defines; every unknown path is an application-level `404`; payloads are sanitized; there is no route that returns data without a request body | **Observed, not theoretical.** The ALB's own access logs for one afternoon hold 174 requests, of which 128 are a single phpMyAdmin brute-force (`Go Web Pentest Tool`, 16:10 UTC, 404 ×128); the rest include MCP/SSE discovery (`Infrawatch/1.0` → `/api/mcp`, `/mcp`, `/mcp/`, `/sse`), `/.git/config` source-disclosure probes, `zgrab` and a Palo Alto scanner. All reached the app (`target=404`) and all were refused. Evidence: [access-log forensics](evidence/level1-access-log-forensics.md) | a public lab ALB with no WAF, no rate limiting and no auth attracts exactly this; nothing here is a *control*, it is a default | WAF + rate limiting, HTTPS, auth, or an internal-only ALB (L3) |

## 6. How to re-verify (about five commands)

```bash
# IAM and instance hardening, straight from the deployed stack
aws cloudformation get-template --stack-name IncidentTriage-level1 \
  --query 'TemplateBody.Resources' | jq '."InstanceRoleDefaultPolicy1531605C"'
aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[].Instances[].MetadataOptions'
aws ec2 describe-security-groups --group-ids "$APP_SG" --query 'SecurityGroups[].IpPermissions'

# no static keys anywhere in the repo (also enforced in CI by bandit + infra tests)
grep -rInE "AKIA[0-9A-Z]{16}|aws_secret_access_key" --include='*.py' . || echo clean

uv run pytest tests/infra -q      # asserts no static keys, private instance, SG-by-reference, scoped IAM
scripts/cleanup.sh --check        # proves no resource outlives a teardown
```

## 7. Out of scope here (Level 2 / 3)

Container/non-root hardening and image scanning (L2) · GitHub OIDC instead of human
credentials (L2) · least-privilege deploy roles per environment (L2) · IAM Access
Analyzer, AWS Config rules, GuardDuty, WAF, HTTPS (L3) · secrets injected at runtime
(L2/3) · multi-region trail and KMS CMK for audit data (L3).
