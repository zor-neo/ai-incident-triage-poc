# ADR 0001: Run the application on a single EC2 instance behind an ALB (Level 1)

- **Status:** Accepted (Level 1)
- **Date:** 2026-10-17
- **Deciders:** Justin (portfolio author)

## Context

Level 1 of the portfolio is explicitly about proving that I can build a secure,
repeatable AWS environment and troubleshoot a running application. The same
application is deliberately re-delivered on ECS Fargate in Level 2. The compute
choice for Level 1 therefore has to optimize for *understanding and
troubleshooting surface*, not for operational convenience.

Options considered:

1. Single EC2 instance behind an Application Load Balancer.
2. ECS on Fargate from the start.
3. AWS App Runner / Lambda.

## Decision

Run the FastAPI application as a systemd-managed service on a single
`t4g.small` Amazon Linux 2023 (arm64) instance in a private subnet, fronted by a
public Application Load Balancer. Instance state (nothing important) is not
persistent; the instance is fully reproducible from CDK + user data.

## Rationale

- A single instance exposes the primitives I want to be able to explain in an
  interview: security groups, instance profiles, user data, systemd units, the
  CloudWatch agent, target group health, and IMDSv2.
- Failure modes are observable and fixable by hand (SSM Session Manager, log
  files, `systemctl status`), which is the material for the Level 1 incident
  report and troubleshooting lab.
- `t4g.small` (arm64) is the cheapest sensible size and is retired/recreated
  per lab session, so IAM, bootstrap, and teardown are exercised repeatedly.

## Consequences

- The instance is a single point of failure. This is acceptable (and honest)
  for a lab; it is fixed structurally in Level 2 by an ECS service with two
  tasks and a rolling deployment.
- No auto scaling. Scaling is deliberately out of scope until Level 2/3.
- The instance runs as a non-root `appuser` with `NoNewPrivileges`, `PrivateTmp`
  and `ProtectSystem=full`, so the "just SSH in and edit it" habit is not
  available: changes go through CDK or an explicit lab exercise.

## Alternatives rejected

- **Fargate now** – would hide exactly the fundamentals Level 1 is meant to
  prove and duplicate Level 2 before Level 1 is solid.
- **App Runner / Lambda** – least configuration, but the portfolio's value is in
  showing the network, IAM, and instance lifecycle explicitly.
