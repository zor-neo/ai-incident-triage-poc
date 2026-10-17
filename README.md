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
| **1 - CloudOps Foundation** | "I can build a secure, repeatable AWS environment and troubleshoot a running application." | EC2 behind an ALB | ✅ **complete and deployed** — live in `us-east-1` while this is being written ([what that costs](#cost--what-is-being-charged-right-now)) |
| **2 - Delivery Engineer** | "I can build a secure CI/CD pipeline that delivers a containerized service." | ECS on Fargate + GitHub OIDC | ⏳ planned |
| **3 - Production-Minded CloudOps** | "I can operate a service with measurable reliability and safe change practices." | ECS + rollout alarms + auto-rollback | ⏳ planned |

> **Live build note.** The deployed instance answers `GET /health` with
> `"version": "5d3733a"`. Commits since then changed docs, tests, CI and scripts
> only — `git diff 5d3733a..HEAD -- app/` is empty — so the running *code* is HEAD,
> while the reported version still names the commit that last shipped an artifact.
> A new commit would replace the instance on the next deploy by design
> ([ADR-0006](docs/adr/0006-replace-instance-on-user-data-change.md)).

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
    SNS["SNS alerts + budget notices<br/>email subscription PendingConfirmation"]
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
[`docs/adr/`](docs/adr).

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
infra/      AWS CDK (Python): app.py, cdk.json, stacks/level1_ec2_stack.py
docs/       adr/, runbooks/, incidents/, network-design.md, security-and-threat-model.md
scripts/    smoke_test.sh, cleanup.sh  (deliverables, referenced below)
scripts/adhoc/  one-off session scripts (SSM drills, probes, deploy verification)
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
- **Measured, not assumed.** A deliberate `systemctl stop` fault injection showed
  the target going `unhealthy` in 58s and the alarm firing **6m44s** later, with
  zero alerts delivered (the SNS subscription was `PendingConfirmation`). After
  tuning (15s health check, alarm on the first datapoint) the same fault pages in
  **1m25s** and clears in 43s — see
  [INC-0002](docs/incidents/0002-fault-detection-latency-and-unconfirmed-alert-path.md).
- Runbook and game-day checklist: [`docs/runbooks/level1-runbook.md`](docs/runbooks/level1-runbook.md).
- Incident reports, written from live first-hand evidence:
  [`docs/incidents/`](docs/incidents) —
  [INC-0001](docs/incidents/0001-stale-code-after-successful-deploy.md) (a green
  `cdk deploy` that shipped nothing) and
  [INC-0002](docs/incidents/0002-fault-detection-latency-and-unconfirmed-alert-path.md).

## Cost — what is being charged right now

**The stack is deployed and idle, and four resources bill continuously whether or
not anyone calls the API.** Idle does not turn the meter off: the clock starts when
the resource is created and stops only when it is destroyed.

| Resource | Rate (us-east-1, on-demand — read from the AWS Pricing API, 2026-10-17) | Cost if left up |
| :--- | :--- | ---: |
| Application Load Balancer | $0.0225/hr, plus $0.008 per LCU-hr of traffic | $16.43/mo |
| NAT gateway | $0.045/hr, plus $0.045 per GB processed | $32.85/mo |
| EC2 `t4g.small` | $0.0168/hr (billed per second, 60s minimum) | $12.26/mo |
| EBS `gp3` 8 GB | $0.08/GB-month | $0.64/mo |
| **Total while idle** | **$0.0852/hr** | **≈$62/mo · $2.04/day** |

Everything else is usage-based or free while idle, because it was chosen that way:

- **DynamoDB** is on-demand — no provisioned capacity, so an idle table costs ~$0;
  you pay per request (fractions of a cent for this workload).
- **VPC endpoints are `Gateway` type (S3, DynamoDB) → $0/hr.** Interface endpoints
  would have added $0.01/hr each (~$15/mo) for the same private access.
- **EBS encryption uses the AWS-managed `aws/ebs` key**, not a customer-managed KMS
  key, saving the ~$1/mo CMK charge.
- The **internet gateway**, the **elastic IP attached to the NAT gateway**, the
  **first CloudTrail management-events trail**, the **first two AWS Budgets**, and
  **SNS email** are all free; the 5 custom metrics and 3 alarms sit inside the
  always-free allowances.
- **Usage-based and measured, tiny here:** Bedrock per-token (a real triage used 200
  input + 57 output tokens, ~$0.0001), NAT data processing, ALB LCUs, CloudWatch
  Logs ingestion at $0.50/GB with 7-day retention (~cents/day at this volume), and a
  few MB of S3 logs.

**What the account itself reports:** Cost Explorer has posted **$0.00** for October
(billing line items lag the usage by hours), and its forecast for the month is
**$2.098** — which matches one day of the $2.04/day burn above. The stack's own
**$10 monthly budget** (80% actual, 100% forecasted) reads `HEALTHY`, `ActualSpend
0.0`, `ForecastedSpend 2.098`.

> ⚠️ Those budget notifications ride the alert SNS topic whose only subscriber is
> still `PendingConfirmation` ([INC-0002](docs/incidents/0002-fault-detection-latency-and-unconfirmed-alert-path.md)) —
> so **the budget is currently a silent guardrail**, exactly like the alarms. At
> $2.04/day the forecast would cross the $10 threshold after roughly five days
> uptime; confirming that email is what turns all of this into a real signal.

`scripts/cleanup.sh` destroys the stack and then asserts nothing project-scoped
survived, so the hourly burn ends with the teardown. `CDKToolkit` and its asset
bucket are intentionally left behind and cost ~$0.

## Roadmap

- **Level 2:** multi-stage non-root Docker image → ECR (immutable commit-SHA
  tags, scan on push) → ECS Fargate staging + prod, GitHub Actions with OIDC,
  smoke tests, manual production approval.
- **Level 3:** modular CDK constructs per environment, ECS deployment alarms
  with automatic rollback, dashboards, EventBridge deployment events,
  IAM Access Analyzer, AWS Config rules, documented game-day exercises and DR.

## Documentation

- [Product Requirements](product_requirements_document.md)
- [Technical & Architectural Specification](technical_and_architectural_specification.md)
- [Portfolio strategy](AWS%20CloudOps%20_%20DevOps%20Portfolio_%20Three%20Levels%20to%20Job-Ready.md)
- [Deployment transcript](docs/evidence/level1-deployment-transcript.md) — the first
  real deploy to AWS, with the failures it exposed
- [Network design](docs/network-design.md) · [ADRs](docs/adr) · [Runbooks](docs/runbooks) · [Incidents](docs/incidents)
- [Security model & threat model](docs/security-and-threat-model.md) — IAM trust
  policies and permissions read back from the deployed stack, plus accepted risks
  (no API auth, HTTP-only, shared asset bucket) and the Level 2/3 fixes for each
- [Session scripts](scripts/adhoc/README.md) — the one-off scripts that produced
  the numbers in the incidents and this README, each with a header saying when it
  was used and why it is kept
