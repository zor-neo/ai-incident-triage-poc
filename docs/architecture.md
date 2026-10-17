# Level 1 architecture

The deployed Level 1 stack, with the on-demand `us-east-1` rates in force on
2026-10-17 (read from the AWS Pricing API). **Red** components bill every hour the
stack exists; **green** ones cost nothing while idle. The live billing position and
the accounting behind these numbers are in the
[README](../README.md#cost--what-is-being-charged-right-now).

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

## Reading the diagram

- **One way in.** The ALB is internet-facing in public subnets and is the only path
  to the instance. It accepts `:80` from `0.0.0.0/0` and forwards to `:8000` using a
  **security-group reference** — the app SG has no CIDR rule at all
  ([why the instance is private](network-design.md)).
- **Two outbound paths.** AWS API calls leave through the NAT gateway (Bedrock) while
  **S3 and DynamoDB go through gateway VPC endpoints**, which cost $0/hr and keep that
  traffic off the NAT. Choosing gateway endpoints over interface endpoints is worth
  roughly $15/month here ([ADR-0002](adr/0002-nat-and-gateway-endpoints-for-egress.md)).
- **The application never holds credentials.** It calls DynamoDB, Bedrock, CloudWatch
  and Logs with short-lived credentials from the instance profile; the role is scoped
  to one table and one model ([permissions verbatim](security-and-threat-model.md)).
- **Everything leaves a trail.** CloudTrail records management events, the ALB writes
  access logs to S3, and the app writes structured JSON logs with `request_id`
  correlation to a 7-day log group.
- **The alert path was the weak link; it is now armed and checkable.** Both the
  alarms and the AWS Budget notify an SNS topic whose only email subscriber sat in
  `PendingConfirmation` from the first deploy, so nothing paged anyone — including
  through a real fault drill ([INC-0002](incidents/0002-fault-detection-latency-and-unconfirmed-alert-path.md)).
  The subscription is confirmed, and `scripts/adhoc/verify_alert_path.sh` fails
  loudly if any subscription is ever pending again, so this cannot silently return.

## What was actually deployed

Read back from the running stack on 2026-10-17 (`aws cloudformation
get-template` / `describe-*`), not from the source:

| Component | Live identifier |
| :--- | :--- |
| Stack | `IncidentTriage-level1` in `us-east-1`, account `685134815483` |
| Entry point | `http://Incide-Alb16-42G9qBLXchCg-1665380019.us-east-1.elb.amazonaws.com` |
| Target group | `Incide-AppTa-53LUD34DALJG` (health check `/health`, 15s interval, 3 unhealthy / 2 healthy) |
| Compute | `i-003f1651c312481d4`, `t4g.small`, private subnet `us-east-1a`, no public IP, `KeyName: null` |
| Identity | instance profile `IncidentTriage-level1-AppInstanceInstanceProfileC6B6EFAA-…` (IMDSv2 required) |
| State | DynamoDB `IncidentTriage-level1` (on-demand, SSE enabled) |
| Model | Bedrock `amazon.nova-micro-v1:0` |
| Logs | `/aws/ec2/incident-triage` (7-day retention) |
| Metrics | namespace `IncidentTriage`, dimension `Environment=level1` |
| Alerts | SNS topic `IncidentTriage-alerts-level1`, email subscription **confirmed** 2026-10-17 (was `PendingConfirmation` — [INC-0002](incidents/0002-fault-detection-latency-and-unconfirmed-alert-path.md)) |
| Audit | CloudTrail `IncidentTriage-level1` + `incidenttriage-level1-*` S3 buckets |

Cost and billing detail: [README § Cost](../README.md#cost--what-is-being-charged-right-now).
Threat model: [security-and-threat-model.md](security-and-threat-model.md).
Decisions and trade-offs: [ADRs](adr).
