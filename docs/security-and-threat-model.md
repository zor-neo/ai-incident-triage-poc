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

## 7. Out of scope here (Level 3)

IAM Access Analyzer, AWS Config rules, GuardDuty, WAF, HTTPS via ACM, a
multi-region trail and a KMS CMK for audit data. (The Level 2 items previously
listed here — container hardening and image scanning, GitHub OIDC instead of human
credentials, least-privilege deploy roles, runtime secret injection — are now
implemented and evidenced in section 8.)

## 8. Level 2: the pipeline-to-production boundary

Level 2 adds a build system, a registry, an orchestrator and a credential that acts
without a human. That is a new trust boundary, so it gets its own model. Everything
below was read from the deployed stacks and from live API calls on 2026-10-18.

### 8.1 The four identities, and why none is interchangeable

| Identity | Trusted principal | Can | Explicitly cannot |
| :--- | :--- | :--- | :--- |
| `TaskExecutionRole-{staging,prod}` | `ecs-tasks.amazonaws.com` | pull the image from one repository, write to one log group, read **one** SecureString (`kms:Decrypt` constrained by `kms:ViaService`) | touch DynamoDB, invoke Bedrock, push images |
| `TaskRole-{staging,prod}` | `ecs-tasks.amazonaws.com` | `PutItem`/`GetItem`/`UpdateItem` on its own table, `bedrock:InvokeModel` on one model, `PutMetricData` in one namespace | pull images, write logs, read the secret, `DeleteItem`, `Scan`, `Query` |
| `incident-triage-level2-github-deploy` | GitHub OIDC (two pinned subjects) | push images, `RegisterTaskDefinition`, `UpdateService` on these two services, `PassRole` for exactly four role ARNs | run CloudFormation, read DynamoDB or Bedrock, change IAM, create users |
| `admin` (human) | — | everything | is not part of any stack, is not used by the application, and is the only identity that can change infrastructure |

`tests/infra/test_level2_stack.py` asserts the split: the task role's actions
contain no `ecr:`, `logs:`, `ssm:` or `kms:`, and the deploy role's contain no
`dynamodb:` and no `bedrock:`.

### 8.2 Verified Level 2 controls

| Control | Evidence (2026-10-18) |
| :--- | :--- |
| No long-lived credentials anywhere | zero `AWS::IAM::User` / `AWS::IAM::AccessKey` in either stack; the pipeline authenticates purely by OIDC |
| OIDC trust is subject-pinned, with no wildcard | `StringLike` lists exactly two GitHub immutable subjects — the deploy branch and the `production` environment ([evidence](evidence/level2-oidc-subject.md)) |
| `PassRole` blast radius bounded | one `iam:PassRole` statement, 4 explicit role ARNs, conditioned on `iam:PassedToService = ecs-tasks.amazonaws.com` |
| Images cannot be silently replaced | ECR `ImageTagMutability: IMMUTABLE` + `ScanOnPush: true` |
| Nothing unscanned is promoted | scan gate blocks HIGH/CRITICAL and **fails closed** on FAILED/UNSUPPORTED/unreadable scans; the only bypass is a dated, owned entry in `security/scan-exceptions.json` |
| Tasks are private | `AssignPublicIp: DISABLED`; the task SG has exactly one inbound rule, `:8000` from the ALB SG **by reference**, no CIDR |
| Environments cannot read each other's secret | `staging` key against the prod endpoint → `401`; distinct per-environment SecureStrings |
| The secret is never in the template | no `AWS::SSM::Parameter` resource exists in the stack; the task definition references an imported ARN |
| Least privilege is enforced by failure | the first pipeline runs were denied by STS and by `ecr:DescribeRepositories` until the role was given *exactly* what it needed — evidence that the policy is not wider than the job |

### 8.3 Supply chain

- **Build input:** `requirements.lock.hashed.txt`, a full closure with SHA-256
  hashes installed with `pip install --require-hashes --no-deps`. A substituted
  wheel fails the build.
- **Build output:** single image manifest (no attestation index), tagged with the
  commit SHA (never `latest`), in an immutable repository — so a tag cannot be
  repointed at different bytes after the fact.
- **Verification:** ECR basic scanning on push, then the gate. The gate's policy is
  fail-closed by design; the exceptions file is the explicit, expiring, owned
  escape hatch ([ADR 0007](adr/0007-hashed-lock-and-single-manifest-image.md)).
- **Residual:** the base image (`python:3.12-slim`) currently carries three HIGH
  CVEs (zlib, glibc) for which Debian has **no fixed version** —
  `apt-cache policy` reports the installed versions as the candidates. They are
  excepted with reachability arguments and a review date, not ignored. This is the
  honest state of a Debian-based image in this window.

### 8.4 Risks added at this level

| ID | Scenario | Control now | Residual risk | Next level |
| :--- | :--- | :--- | :--- | :--- |
| RISK-12 | A compromised pipeline widens its own access | the deploy role cannot run CloudFormation, change IAM, or read data; `PassRole` is bounded; the OIDC subject is pinned | the pipeline can push an image and deploy it, so it can ship code | short-lived scoped sessions, and a separate production account so the pipeline credential cannot reach prod IAM |
| RISK-13 | A malicious or careless image is promoted | scan gate (HIGH/CRITICAL, fail-closed), immutable SHA tags, staging deploy + smoke test before the production gate | the gate sees OS packages, not application logic; a smoke test is a contract check, not a security test | signing (cosign) + admission control, and SAST/dependency scanning on the source |
| RISK-14 | The OIDC trust policy is too permissive, or too brittle | pinned to two immutable subjects, asserted by a test; no wildcard, no classic-name form | *brittle in the other direction* — a repository rename or a new subject format breaks a deploy, as it did twice here | monitor for GitHub subject-format changes; consider an org-level reusable workflow that centralises the trust |
| RISK-15 | The runtime secret leaks (logs, image, template, task definition) | created out of band, imported not stored, injected via `secrets`, never echoed, `401` across environments, `hmac.compare_digest` comparison | anyone who can read the parameter can read the value; there is no rotation schedule | Secrets Manager with rotation, and CloudTrail alerting on `ssm:GetParameter` against these paths |

### 8.5 Verifying Level 2 yourself

```bash
# the deploy role's exact trust policy and permissions
aws iam get-role --role-name incident-triage-level2-github-deploy

# tasks are private and immutable-tagged
aws ecs describe-services --cluster incident-triage-level2 \
  --services incident-triage-staging incident-triage-prod
aws ecr describe-repositories --repository-names incident-triage

# the secret is injected and isolated (expect 200 / 401 / 401)
KEY=$(aws ssm get-parameter --name /incident-triage/level2/staging/internal-api-key \
        --with-decryption --query Parameter.Value --output text)
curl -s -H "X-Internal-Key: $KEY" http://<alb>:8080/internal/config
curl -s -o /dev/null -w '%{http_code}\n' -H "X-Internal-Key: $KEY" http://<alb>/internal/config

uv run pytest tests/infra -q      # 26 assertions covering all of the above
scripts/ecr_scan_gate.py incident-triage <tag>   # the supply-chain gate
```
