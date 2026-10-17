# AI Incident & Log Triage Service

An internal engineering API that ingests application error logs, **scrubs
secrets and PII**, persists the raw incident *before* touching any AI model, and
then enriches the record with an **AI triage note** (summary, probable cause,
first action) via **Amazon Bedrock** — degrading gracefully when the model is
slow, throttled, or unavailable.

This repository is a **three-level CloudOps / DevOps portfolio**. It is the same
small application delivered three times, each level adding the delivery,
security, reliability, and operations practices of a real service. The business
logic is deliberately simple; **the engineering system is the portfolio.**

| Level | What it proves | Compute | Status |
| :--- | :--- | :--- | :--- |
| **1 - CloudOps Foundation** | "I can build a secure, repeatable AWS environment and troubleshoot a running application." | EC2 behind an ALB | ✅ **complete** — built, deployed and operated on 2026-10-17, then destroyed ([INC-0001](docs/incidents/0001-stale-code-after-successful-deploy.md), [INC-0002](docs/incidents/0002-fault-detection-latency-and-unconfirmed-alert-path.md), [forensics](docs/evidence/level1-access-log-forensics.md)) |
| **2 - Delivery Engineer** | "I can build a secure CI/CD pipeline that delivers a containerized service." | ECS on Fargate + GitHub OIDC | ✅ **complete** — [public repo](https://github.com/zor-neo/ai-incident-triage-poc), pipeline green through a manual production gate, then destroyed ([INC-0003](docs/incidents/0003-unscannable-image-blocked-the-scan-gate.md), [INC-0004](docs/incidents/0004-bad-release-promoted-to-staging.md)) |
| **3 - Production-Minded CloudOps** | "I can operate a service with measurable reliability and safe change practices." | ECS + rollout alarms + auto-rollback | ⏳ planned |

> **Current state: nothing is deployed, on purpose.** Both levels were built,
> deployed, verified against live AWS and then destroyed, so cloning this repository
> does not leave resources or a bill running. Everything documented here is an
> observation that was actually made against a live environment, and the evidence
> (alarm history, CloudTrail records, access logs, task stop reasons) is in
> [`docs/`](docs). `scripts/cleanup.sh --check` currently reports every
> project-scoped check empty — that output *is* the teardown evidence.

> **Level 2, in one paragraph.** The same service, now built into an immutable
> container image tagged with the commit SHA, scanned before it can be promoted,
> and run as two Fargate services (staging and prod) behind one ALB. Releases go
> through GitHub Actions authenticated by **OIDC — there are no long-lived AWS keys
> anywhere** — and production sits behind a required-reviewer gate. The pipeline
> holds only the power to move an artifact: it cannot run CloudFormation, read the
> database, or change IAM. Deploy paths, rollback and teardown:
> [`docs/runbooks/level2-runbook.md`](docs/runbooks/level2-runbook.md).

> **Reproducibility was tested, not assumed.** After the first teardown, the whole
> Level 2 sequence in the runbook was run again **from an empty account** —
> create the secret, deploy the registry, build and push the image, pass the scan
> gate, deploy the services, smoke-test both environments — and it came up healthy
> with the secret injected and the two environments isolated. Wall clock: about
> 20 minutes, most of it waiting for an ECS service to stabilise, which is inside
> the "reviewer can deploy it in under 30 minutes" target. It was then destroyed
> again and the teardown re-verified. The honest caveat is that this is one
> repetition, on one account, with the CDK bootstrap already present.

## Architecture (Level 1)

Cost annotations are the on-demand `us-east-1` rates in force on 2026-10-17, read
from the AWS Pricing API — **red** components bill every hour the stack exists,
**green** ones cost nothing while idle. See [Cost](#cost--what-is-being-charged-right-now).

```mermaid
flowchart LR
  User["Client / on-call engineer<br/>curl · /docs"] -->|"HTTP :80"| ALB

  subgraph AWS["AWS account 685134815483 · us-east-1 · Project=IncidentTriage"]
    ALB["ALB, internet-facing<br/>$0.0225/hr + $0.008/LCU-hr"]
    EC2["EC2 t4g.small, private subnet<br/>FastAPI · systemd · non-root<br/>$0.0168/hr"]
    EBS["EBS gp3 8 GB<br/>KMS-encrypted (aws/ebs)<br/>$0.08/GB-mo = $0.64/mo"]
    NAT["NAT gateway<br/>$0.045/hr + $0.045/GB"]
    VPE["VPC gateway endpoints<br/>S3 + DynamoDB<br/>$0/hr"]
    DDB[("DynamoDB, on-demand<br/>~$0 while idle")]
    BR["Amazon Bedrock<br/>Nova Micro<br/>per-token"]
    CWL["CloudWatch Logs + 5 custom metrics + 3 alarms<br/>$0.50/GB ingest · 7-day retention"]
    S3[("S3: ALB access logs + CloudTrail<br/>pennies")]
    CT["CloudTrail<br/>management events<br/>first trail $0"]
    SNS["SNS alerts + budget notices<br/>email subscription confirmed 2026-10-17"]
    BUD["AWS Budget: $10<br/>80% actual / 100% forecasted"]
  end

  APIS["Internet / AWS APIs"]

  ALB -->|":8000 · SG reference only"| EC2
  EC2 --- EBS
  EC2 --> VPE
  VPE --> DDB
  EC2 -->|"InvokeModel · 2s/5s hard timeout"| BR
  EC2 -->|logs · PutMetricData| CWL
  EC2 --> NAT
  NAT --> APIS
  BR --> APIS
  ALB -->|access logs| S3
  CT --> S3
  CWL --> SNS
  BUD --> SNS

  classDef burns fill:#3a1d1d,stroke:#ff6b6b,stroke-width:2px,color:#fff
  classDef free fill:#1d3a24,stroke:#6bff8f,stroke-width:2px,color:#fff
  class ALB,EC2,NAT,EBS burns
  class VPE,DDB free
```

Full network and security detail: [`docs/network-design.md`](docs/network-design.md),
[`docs/security-and-threat-model.md`](docs/security-and-threat-model.md), and the ADRs in
[`docs/adr/`](docs/adr). The same diagram with a walk-through of the flows and the live
resource inventory lives in [`docs/architecture.md`](docs/architecture.md).

### Architecture (Level 2)

Delivery changes; the application does not. What is new is that an artifact is built
once, verified, and then promoted by a pipeline that holds no long-lived credentials.

```mermaid
flowchart LR
  Dev["Developer"] -->|pull request| GH["GitHub"]
  GH --> Checks["ruff · bandit · pytest · cdk synth + cdk-nag"]
  Checks --> Build["docker build<br/>linux/amd64 · single manifest"]
  Build -->|"tag = commit SHA"| ECR[("ECR · IMMUTABLE<br/>scan on push")]
  ECR --> Gate{"scan gate<br/>HIGH/CRITICAL?"}
  Gate -->|blocked| Stop["pipeline stops"]
  Gate -->|clean or excepted| Stg["ECS Fargate staging :8080"]
  Stg --> Smoke["smoke test"]
  Smoke --> Approve{"production approval<br/>required reviewer"}
  Approve -->|approved| Prod["ECS Fargate prod :80"]
  Prod --> Smoke2["smoke test"]
  GH -.->|OIDC token, no static keys| Role["deploy role<br/>push image + move service<br/>nothing else"]
  Role --> ECR
  Role --> Stg
  Role --> Prod
  Stg --> ALB["ALB · :80 prod / :8080 staging"]
  Prod --> ALB

  classDef burns fill:#3a1d1d,stroke:#ff6b6b,stroke-width:2px,color:#fff
  classDef free fill:#1d3a24,stroke:#6bff8f,stroke-width:2px,color:#fff
  classDef gate fill:#1d2b3a,stroke:#6bb8ff,stroke-width:2px,color:#fff
  class ECR,Stg,Prod,ALB burns
  class Role free
  class Gate,Approve gate
```

The full Level 2 diagram, its cost annotations, the live resource inventory, and the
three-role split are in [`docs/architecture.md`](docs/architecture.md#level-2--containerised-delivery).

## What the service does

- `POST /incidents` — accepts a service, environment, severity, and raw error
  log. Sanitizes secrets/PII, writes the incident as `PENDING_TRIAGE`, calls
  Bedrock, then updates it to `TRIAGED` (**201**) or `AI_UNAVAILABLE` (**202**).
- `GET /incidents/{incident_id}` — returns the stored incident, triage, and
  operational metrics.
- `GET /health` — lightweight health/version endpoint for the ALB target group.
- `GET /docs` — FastAPI's OpenAPI UI.

### The resilience contract

1. Generate `incident_id` + `request_id`, scrub the log text.
2. Persist the sanitized incident **first** (`PENDING_TRIAGE`).
3. Invoke Bedrock with `connect_timeout=2`, `read_timeout=5`,
   `total_max_attempts=1` — one slow model call can never pile up behind the ALB.
4. On success → `TRIAGED`, emit `TriageSuccess`, `BedrockLatencyMs`,
   `TokensUsed`, return **201**.
5. On any of *timeout / throttling / access denied / malformed JSON* → store
   `AI_UNAVAILABLE` with fallback guidance, emit `TriageFallback`, return
   **202**. **The incident is never lost and the API never 5xx's because the
   model misbehaved.**

### Sanitization

Every `error_log` is scrubbed before it reaches logs, DynamoDB, or Bedrock:

| Pattern | Becomes |
| :--- | :--- |
| AWS access key id (`AKIA…` / `ASIA…`) | `[REDACTED_AWS_KEY]` |
| `aws_secret_access_key=…` context | `[REDACTED_AWS_SECRET]` |
| `Bearer …`, `api_key: …` | `[REDACTED_SECRET]` |
| `password|secret|token = …` | `[REDACTED_SECRET]` |
| Email addresses | `[REDACTED_EMAIL]` |
| IPv4 addresses | `[REDACTED_IP]` |

## Repository layout

```text
app/        FastAPI service (routers, sanitizer, Bedrock client, DynamoDB repo)
tests/      unit/ (moto + stubbed Bedrock), infra/ (CDK assertions), smoke/ (live)
infra/      AWS CDK (Python): app.py, cdk.json, stacks/level1_ec2_stack.py,
            stacks/level2_ecs_stack.py (registry + ECS Fargate services)
docs/       adr/, runbooks/, incidents/, evidence/, network-design.md,
            architecture.md, security-and-threat-model.md
scripts/    cleanup.sh, smoke_test.sh, level2_bootstrap.sh, deploy_ecs_image.py,
            ecr_scan_gate.py  (deliverables, referenced below)
scripts/adhoc/  one-off session scripts (SSM drills, probes, rollback lab)
security/   scan-exceptions.json (time-boxed, owned CVEs the gate lets through)
Dockerfile  multi-stage, non-root (UID 10001), hashed dependency install
.github/    ci.yml (PR gate, also called by the pipeline) + pipeline.yml (delivery)
```

## Prerequisites

- Python 3.12+ (Docker image pins 3.12; local 3.13 works)
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- Node.js (for the AWS CDK CLI) — `npm i -g aws-cdk`
- AWS CLI v2 with credentials for the target account, and **Bedrock model
  access for `amazon.nova-micro-v1:0` in `us-east-1`**

## Quickstart (deploy in under 30 minutes)

```bash
# 1. Dependencies (app + dev + infra groups)
uv sync --all-groups

# 2. Local checks without touching AWS
uv run ruff check app tests infra
uv run pytest                       # unit + infra assertions
uv run bandit -q -r app             # SAST
# CI (.github/workflows/ci.yml) runs these three plus `cdk synth`, where cdk-nag
# fails the build on any finding that is not explicitly acknowledged in the stack.
# 3. One-time per account/region
cd infra && uv run --no-sync cdk bootstrap && cd ..

# 4. Preview and deploy
cd infra
uv run --no-sync cdk diff
uv run --no-sync cdk deploy          # ~4 min; app changes replace the instance
```

Then:

```bash
BASE_URL=$(aws cloudformation describe-stacks --stack-name IncidentTriage-level1 \
  --query "Stacks[0].Outputs[?OutputKey=='ApplicationUrl'].OutputValue" --output text)

curl -s "$BASE_URL/health"           # must show the git SHA you just deployed
scripts/smoke_test.sh "$BASE_URL"
```

A deploy is finished only when `/health`'s `version` matches `git rev-parse --short
HEAD`. `UPDATE_COMPLETE` means the stack matches the template, not that the new
code is running — the first real deploy of this repo proved that the hard way
([INC-0001](docs/incidents/0001-stale-code-after-successful-deploy.md)).

curl -s -X POST "$BASE_URL/incidents" -H 'Content-Type: application/json' -d '{
  "service": "payment-worker", "environment": "prod", "severity": "ERROR",
  "error_log": "botocore.exceptions.ClientError: AccessDenied for key AKIAIOSFODNN7EXAMPLE"
}' | python -m json.tool
```

### Tear down (do this when the lab session ends)

```bash
scripts/cleanup.sh            # cdk destroy + orphan verification (non-zero exit on a leak)
scripts/cleanup.sh --check    # verification only, destroys nothing (safe to run anytime)
```

`--check` also **verifies the verifier**: run it while the stack is up and every
project-scoped section should list its live resources; after a teardown the same
sections must all read `none`. Region-wide inventory is printed separately and
never fails the run, because this account also hosts earlier labs.

Level 2 has one extra step because the secret is deliberately not CDK's to delete:

```bash
scripts/level2_bootstrap.sh --delete   # removes the out-of-band SSM parameters
```

## Deploying Level 2

The full procedure, including the ordering constraint, is in
[`docs/runbooks/level2-runbook.md`](docs/runbooks/level2-runbook.md). The short
version, and the order matters:

```bash
scripts/level2_bootstrap.sh                 # 1. the out-of-band secret (ADR 0010)
cd infra && cdk deploy IncidentTriage-level2-registry && cd ..
                                            # 2. the registry must exist first...
SHA=$(git rev-parse --short HEAD)
docker build --platform linux/amd64 --provenance=false --sbom=false -t "$URI:$SHA" .
docker push "$URI:$SHA"                     # 3. ...before an image can be pushed...
uv run --no-sync python scripts/ecr_scan_gate.py incident-triage "$SHA"
cd infra && cdk deploy IncidentTriage-level2 -c imageTag="$SHA"
                                            # 4. ...before a service can pull one
```

Then merge to `main` and let the pipeline take over. The four ordering steps are not
ceremony: a stack that creates the repository and the service together cannot be
deployed at all the first time, because CloudFormation waits for a service to
stabilise and the image it needs can only be pushed after the repository exists
([ADR 0008](docs/adr/0008-registry-stack-and-two-fargate-services.md)).

Notifications into the pipeline are real: the `production` Environment requires a
reviewer, so a release is *paused* until a human approves it, and the approving
identity is recorded in the deployment history.

## Testing

| Suite | Command | Notes |
| :--- | :--- | :--- |
| Unit | `uv run pytest tests/unit` | moto-mocked DynamoDB, stubbed Bedrock; no AWS calls |
| Infra | `uv run pytest tests/infra` | CDK template assertions (private instance, SG-by-reference, scoped IAM, no static keys) |
| Smoke (live) | `SMOKE_BASE_URL=$BASE_URL uv run pytest tests/smoke` | hits a deployed environment |

## Security posture (Level 1)

- **No static credentials.** The instance uses an IAM role; CI will use GitHub
  OIDC in Level 2. There are zero `AWS::IAM::User`/`AWS::IAM::AccessKey`
  resources, and a test asserts it.
- **Least privilege.** DynamoDB actions limited to `PutItem/GetItem/UpdateItem`
  on the table; `bedrock:InvokeModel` limited to the model + inference-profile
  ARNs; `cloudwatch:PutMetricData` constrained by a namespace condition;
  Session Manager permissions limited to the documented minimum.
- **Network.** Only port 80 is open on the ALB. The app security group accepts
  traffic **only by reference** from the ALB security group, never from a CIDR.
- **The exposure is real, not hypothetical.** The ALB's own access logs show the
  public endpoint being scanned continuously: a 128-request phpMyAdmin sweep in
  one minute (`Go Web Pentest Tool`), MCP/SSE endpoint discovery, `/.git/config`
  disclosure probes, `zgrab` and a Palo Alto scanner — all reaching the
  application and all refused with a `404`. None of it is a *control*; it is the
  app's route table doing its job. Evidence and analysis:
  [`docs/evidence/level1-access-log-forensics.md`](docs/evidence/level1-access-log-forensics.md).
- **`cdk-nag` (`AwsSolutionsChecks`) runs on every `cdk synth`.** Every accepted
  finding has a written justification in the stack; the build fails on anything
  not explicitly acknowledged.
- **Instance hardening.** IMDSv2 required, encrypted EBS, hardened non-root
  systemd unit, no SSH (Session Manager only), CloudTrail + file validation on.

## Observability & operations

- Structured JSON logs (`request_id`, `incident_id`, `git_sha`, `status`,
  `ai_latency_ms`) shipped to `/aws/ec2/incident-triage` (7-day retention).
- Alarms: ALB **unhealthy targets**, target **5xx**, EC2 **CPU** — all wired to
  the alert SNS topic.
- Custom business metrics: `TriageSuccess`, `TriageFallback`
  (and `TriageFallbackByReason`), `BedrockLatencyMs`, `TokensUsed`.
- **Measured, not assumed — and then the measurement was audited.** A deliberate
  `systemctl stop` fault injection showed the target going `unhealthy` in ~78s and
  the alarm firing **6m44s** later — and exposed a worse problem: **zero alerts
  were delivered**, because the SNS email subscription was still
  `PendingConfirmation`. Tuning it (15s health check, alarm on the first datapoint)
  dropped the datapoints the alarm needs from 3 → 2 → 1 — a change the alarm
  history records in its own reason strings. But re-reading that history a day
  later showed the published tuned figure (**1m25s**) did not reconcile with
  primary evidence: a consistent **~3-minute** CloudWatch pipeline lag dominates,
  and the v3 number had been measured across the very `cdk deploy` that replaced
  the instance. The honest end-to-end figure is **~3 minutes**, and the correction
  is written into
  [INC-0002](docs/incidents/0002-fault-detection-latency-and-unconfirmed-alert-path.md)
  (status: **resolved**) rather than quietly edited. The subscription is confirmed,
  and the path is now checked by `scripts/adhoc/verify_alert_path.sh` instead of
  trusted.
- Runbook and game-day checklist: [`docs/runbooks/level1-runbook.md`](docs/runbooks/level1-runbook.md).
- Incident reports, written from live first-hand evidence:
  [`docs/incidents/`](docs/incidents) —
  [INC-0001](docs/incidents/0001-stale-code-after-successful-deploy.md) (a green
  `cdk deploy` that shipped nothing) and
  [INC-0002](docs/incidents/0002-fault-detection-latency-and-unconfirmed-alert-path.md).

## Cost — what is being charged right now

**Right now: nothing.** Both stacks have been destroyed, and
`scripts/cleanup.sh --check` reports every project-scoped check empty, so the
hourly burn below has stopped. What follows is what *was* being charged while each
level was up, and what would be charged again on the next deploy.

**While deployed, four resources bill continuously whether or not anyone calls the
API.** Idle does not turn the meter off: the clock starts when
the resource is created and stops only when it is destroyed.

| Resource | Rate (us-east-1, on-demand — read from the AWS Pricing API, 2026-10-17) | Cost if left up |
| :--- | :--- | ---: |
| Application Load Balancer | $0.0225/hr, plus $0.008 per LCU-hr of traffic | $16.43/mo |
| NAT gateway | $0.045/hr, plus $0.045 per GB processed | $32.85/mo |
| EC2 `t4g.small` | $0.0168/hr (billed per second, 60s minimum) | $12.26/mo |
| EBS `gp3` 8 GB | $0.08/GB-month | $0.64/mo |
| **Public IPv4 addresses (3)** | $0.005/hr **each** — 1 on the NAT gateway, 2 as ALB node addresses | $10.95/mo |
| **Total while idle** | **$0.1002/hr** | **≈$73/mo · $2.40/day** |

**Level 2 adds a second, independent environment:**

| Resource | Rate | Cost if left up |
| :--- | :--- | ---: |
| 2 × Fargate task, 0.25 vCPU / 0.5 GiB | $0.04048/vCPU-hr + $0.004445/GB-hr | $17.98/mo |
| ALB (second one, carries both environments) | $0.0225/hr + LCU | $16.43/mo |
| NAT gateway | $0.045/hr + $0.045/GB | $32.85/mo |
| Public IPv4 addresses (3: 1 NAT + 2 ALB) | $0.005/hr each | $10.95/mo |
| ECR storage | ~$0.10/GB-month (308 MB image, ~6 tags) | ~$0.20/mo |
| **Total while idle** | **$0.108/hr** | **≈$79/mo · $2.60/day** |

**Both levels idle together: ≈ $0.208/hr ≈ $5/day ≈ $152/month.** That is the
number to have in mind before leaving either stack up; `scripts/cleanup.sh` is what
ends it.

Everything else is usage-based or free while idle, because it was chosen that way:

- **DynamoDB** is on-demand — no provisioned capacity, so an idle table costs ~$0;
  you pay per request (fractions of a cent for this workload).
- **VPC endpoints are `Gateway` type (S3, DynamoDB) → $0/hr.** Interface endpoints
  would have added $0.01/hr each (~$15/mo) for the same private access.
- **EBS encryption uses the AWS-managed `aws/ebs` key**, not a customer-managed KMS
  key, saving the ~$1/mo CMK charge.
- The **internet gateway**, the **first CloudTrail management-events trail**, the
  **first two AWS Budgets**, and **SNS email** are all free; the alarms and custom
  metrics sit inside the always-free allowances.
- **Public IPv4 addresses are not free, and this is easy to get wrong.** Since
  1 February 2024 AWS bills **$0.005/hr for every public IPv4 address**, attached or
  not — which for these stacks means the NAT gateway's Elastic IP *and* the ALB's
  per-AZ node addresses. An earlier version of this section claimed the NAT's
  address was free; it was wrong, and the correction is worth ~$11/month here.
  Note this is the **published rate**: Cost Explorer had not posted any
  EC2/VPC line items when this was written, so it is arithmetic from the rate, not
  an observed charge. Treat `USE1-PublicIPv4:InUseAddress` on the bill as the
  confirmation.
- **Usage-based and measured, tiny here:** Bedrock per-token (a real triage used 200
  input + 57 output tokens, ~$0.0001), NAT data processing, ALB LCUs, CloudWatch
  Logs ingestion at $0.50/GB with 7-day retention (~cents/day at this volume), ECR
  storage, and a few MB of S3 logs.

**What the account itself reports:** Cost Explorer has posted **$0.00** for October
(billing line items lag the usage by hours), and its forecast for the month is
**$2.098** — which matches one day of the $2.04/day burn above. The stack's own
**$10 monthly budget** (80% actual, 100% forecasted) reads `HEALTHY`, `ActualSpend
0.0`, `ForecastedSpend 2.098`.

> **The guardrail is now armed.** Budget notifications ride the same alert SNS
> topic as the CloudWatch alarms, so both were silent while the email subscription
> sat in `PendingConfirmation` ([INC-0002](docs/incidents/0002-fault-detection-latency-and-unconfirmed-alert-path.md)).
> The subscription was confirmed on 2026-10-17, so at $2.04/day the forecasted-spend
> notification will genuinely reach the owner within about five days of uptime
> instead of dying at the topic. Verify any time with
> `scripts/adhoc/verify_alert_path.sh`.

`scripts/cleanup.sh` destroys the stack and then asserts nothing project-scoped
survived, so the hourly burn ends with the teardown. `CDKToolkit` and its asset
bucket are intentionally left behind and cost ~$0.

## Roadmap

- **Level 2 — done.** Multi-stage non-root Docker image → ECR (immutable commit-SHA
  tags, scan on push) → ECS Fargate staging + prod behind one ALB, GitHub Actions
  with OIDC, smoke tests, and a required-reviewer production gate. Deployed, and the
  pipeline has run end to end. Two things it deliberately does *not* do yet:
  automatic rollback, and any logging/metrics beyond logs plus ALB alarms.
- **Level 3:** modular CDK constructs per environment, ECS deployment alarms with
  automatic rollback (the circuit breaker's rollback flag is currently off — see
  [INC-0004](docs/incidents/0004-bad-release-promoted-to-staging.md) for why it is
  the obvious next control), dashboards, EventBridge deployment events, container
  insights, IAM Access Analyzer, AWS Config rules, HTTPS via ACM, and documented
  game-day exercises with a DR decision.

## Documentation

- [Product Requirements](product_requirements_document.md)
- [Technical & Architectural Specification](technical_and_architectural_specification.md)
- [Portfolio strategy](AWS%20CloudOps%20_%20DevOps%20Portfolio_%20Three%20Levels%20to%20Job-Ready.md)
- [Deployment transcript (Level 1)](docs/evidence/level1-deployment-transcript.md) —
  the first real deploy to AWS, with the failures it exposed
- [Architecture](docs/architecture.md) — cost-annotated diagrams for both levels, how
  to read them, and the live identifiers of what was actually deployed
- [Level 2 runbook](docs/runbooks/level2-runbook.md) — deploy, verify, roll back,
  game day, cost, teardown
- [OIDC evidence](docs/evidence/level2-oidc-subject.md) — the exact subject claim the
  deploy role must trust, read from CloudTrail
- [Network design](docs/network-design.md) · [ADRs](docs/adr) · [Runbooks](docs/runbooks) · [Incidents](docs/incidents)
- [Security model & threat model](docs/security-and-threat-model.md) — IAM trust
  policies and permissions read back from the deployed stack, plus accepted risks
  (no API auth, HTTP-only, shared asset bucket) and the Level 2/3 fixes for each
- [Session scripts](scripts/adhoc/README.md) — the one-off scripts that produced
  the numbers in the incidents and this README, each with a header saying when it
  was used and why it is kept
