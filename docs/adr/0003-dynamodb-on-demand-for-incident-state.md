# ADR 0003: DynamoDB (on-demand, single table) for incident state

- **Status:** Accepted (Level 1)
- **Date:** 2026-10-17

## Context

The service must persist an incident *before* calling the AI model (so an
incident is never lost when Bedrock degrades), then update it with triage
output. Access pattern is trivial: write one item, read one item by id.

Options considered:

1. Amazon DynamoDB, on-demand, partition key `incident_id`.
2. Amazon RDS for PostgreSQL.
3. SQLite on the instance's EBS volume.

## Decision

Amazon DynamoDB, `PAY_PER_REQUEST`, table `IncidentTriage-{environment}` with
partition key `incident_id` (string). `RemovalPolicy.DESTROY` in the lab so
teardown is clean.

## Rationale

- No connection pooling, VPC ENI, secrets, or schema migrations to manage for a
  key-value read/write by id — the exact access pattern of this service.
- Reaching DynamoDB through a *free gateway endpoint* keeps it fully private and
  off the NAT gateway (see ADR 0002).
- `PAY_PER_REQUEST` means an idle lab table costs nothing, matching the
  deploy/destroy-per-session workflow.
- Also removes the "RDS outside the VPC / publicly accessible" mistake as a
  possible misconfiguration: the table has no network surface at all.

## Consequences

- Queries beyond "get by id" (e.g. list incidents by service + time) need a GSI
  or a different store; not needed at Level 1.
- **Point-in-time recovery is disabled at Level 1** to keep the lab free
  (cdk-nag `AwsSolutions-DDB3` is acknowledged in the stack with this reason).
  It is enabled, with a documented restore procedure, in the Level 3 DR plan.
- The lab removal policy is `DESTROY`; a production environment would use
  `RETAIN` plus PITR/backups.

## Alternatives rejected

- **RDS** – justified only if relational troubleshooting is itself the goal;
  here it adds cost and a bigger blast radius for no functional gain.
- **SQLite on instance storage** – loses data on instance replacement, which is
  a core Level 1 lab (recovery by recreating the instance), so it would
  contradict the recovery story.
