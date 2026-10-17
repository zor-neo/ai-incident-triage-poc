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
| **1 - CloudOps Foundation** | "I can build a secure, repeatable AWS environment and troubleshoot a running application." | EC2 behind an ALB | ✅ **complete, deployed and torn down** |
| **2 - Delivery Engineer** | "I can build a secure CI/CD pipeline that delivers a containerized service." | ECS on Fargate + GitHub OIDC | ⏳ planned |
| **3 - Production-Minded CloudOps** | "I can operate a service with measurable reliability and safe change practices." | ECS + rollout alarms + auto-rollback | ⏳ planned |

## Architecture (Level 1)

```mermaid
flowchart LR
  User[Client / on-call engineer] -->|HTTP :80| ALB[Application Load Balancer<br/>public subnets]
  ALB -->|:8000, security-group reference| EC2[EC2 t4g.small<br/>private subnet<br/>FastAPI + systemd, non-root]
  EC2 -->|gateway endpoint| DDB[(DynamoDB<br/>IncidentTriage-level1)]
  EC2 -->|InvokeModel, 2s/5s hard timeout| BR[Amazon Bedrock<br/>Nova Micro]
  EC2 -->|CloudWatch agent| CWL[CloudWatch Logs<br/>/aws/ec2/incident-triage]
  EC2 -->|PutMetricData| CWM[CloudWatch metrics + alarms]
  CWM --> SNS[SNS alerts + budget notifications]
  All[AWS API activity] --> CT[CloudTrail]
  IAM[IAM instance role<br/>no static keys] --> EC2
```

Full details: [`docs/network-design.md`](docs/network-design.md) and the ADRs in
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
docs/       adr/, runbooks/, incidents/, network-design.md
scripts/    smoke_test.sh, cleanup.sh
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

# 3. One-time per account/region
cd infra && uv run --no-sync cdk bootstrap && cd ..

# 4. Preview and deploy
cd infra
uv run --no-sync cdk diff
uv run --no-sync cdk deploy          # ~5 min, first boot installs the app
```

Then:

```bash
BASE_URL=$(aws cloudformation describe-stacks --stack-name IncidentTriage-level1 \
  --query "Stacks[0].Outputs[?OutputKey=='ApplicationUrl'].OutputValue" --output text)

curl -s "$BASE_URL/health"
scripts/smoke_test.sh "$BASE_URL"

curl -s -X POST "$BASE_URL/incidents" -H 'Content-Type: application/json' -d '{
  "service": "payment-worker", "environment": "prod", "severity": "ERROR",
  "error_log": "botocore.exceptions.ClientError: AccessDenied for key AKIAIOSFODNN7EXAMPLE"
}' | python -m json.tool
```

### Tear down (do this when the lab session ends)

```bash
scripts/cleanup.sh        # cdk destroy + orphan verification
```

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
- Runbook and game-day checklist: [`docs/runbooks/level1-runbook.md`](docs/runbooks/level1-runbook.md).
- Incident reports: [`docs/incidents/`](docs/incidents).

## Cost

While up, roughly **$0.09/hour** (NAT gateway ~$32/mo, ALB ~$16/mo, `t4g.small`
~$12/mo, plus cents of S3/CloudTrail/Bedrock). A one-day lab is about **$2**. The
stack creates a **$10 monthly AWS Budget** with 80%-actual and
100%-forecasted alerts, and `scripts/cleanup.sh` verifies nothing is orphaned.

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
- [Network design](docs/network-design.md) · [ADRs](docs/adr) · [Runbooks](docs/runbooks) · [Incidents](docs/incidents)
