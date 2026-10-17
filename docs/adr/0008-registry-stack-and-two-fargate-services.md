# ADR 0008: Registry as its own stack, two Fargate services behind one ALB

- **Status:** Accepted (Level 2)
- **Date:** 2026-10-18

## Context

Level 2 replaces the EC2 instance with ECS on Fargate and needs two environments -
staging and production - so that a change can be verified before it is promoted.
That raises three questions: what hosts the image, how many load balancers there
are, and in what order things can be created.

## Decision

1. **The ECR repository is a separate stack** (`IncidentTriage-level2-registry`)
   from the services (`IncidentTriage-level2`).
2. **One internet-facing ALB** carries both environments, on two listeners: port
   80 for prod, port 8080 for staging.
3. **Two task definitions and two services**, sharing a cluster, a log group, a
   task security group and an alert topic, with a separate DynamoDB table per
   environment.
4. A **rolling deployment** with `min_healthy_percent: 100` /
   `max_healthy_percent: 200`, and the circuit breaker **enabled but not rolling
   back** (`rollback=False`).

## Rationale

- **The ordering problem is real and better made explicit than hidden.** The
  registry must exist before an image can be pushed, and the image must exist
  before a service can reach a steady state - CloudFormation waits for ECS
  services to stabilise, so a stack that creates the repository and the service
  together *cannot be deployed at all* the first time: the service waits forever
  for an image that only the deploy could have pushed. Two stacks make the order
  part of the design (`cdk deploy ...-registry`, push, `cdk deploy ...`). The
  alternative - create services at `desired_count=0` and fix it afterwards - hides
  the constraint and needs two deploys anyway.
- **One ALB, two listeners, is the cheap and honest choice for a lab.** A second
  ALB doubles the fixed hourly cost and the public IPv4 count for no extra lesson.
  Port-based separation is obvious in the target groups and in the URLs.
- **Per-environment tables** keep staging data out of production, and make the
  isolation testable: `/internal/config` reports which table the task is bound to.
- **`min_healthy_percent: 100` is what made INC-0004 a non-event.** ECS refuses to
  drain healthy tasks until replacements are healthy, so a bad release fails the
  deployment instead of taking the service down.
- **Not rolling back automatically is a deliberate line between levels.**
  Auto-rollback on CloudWatch deployment alarms is Level 3's "controlled change".
  At this level the failure is detected (alarm + non-zero exit) and the rollback is
  an explicit command, which is easier to observe and explain.

## Consequences

- Two public IPv4 addresses on the ALB and one on the NAT gateway are billed hourly
  whether or not anyone calls the API.
- A stack-level change to the Level 2 stack recreates task definition revisions;
  the pipeline's image tag is therefore tracked as a context value, and a manual
  `cdk deploy` must pass `-c imageTag=<tag>` or it will point the services at a tag
  that may not exist (this happened - see the guard added to
  `scripts/deploy_ecs_image.py`).
- Staging is reachable on a non-standard port, so URLs must be quoted correctly.
  An early version of the URL output used the loop variable after the loop ended
  and emitted `:80` for staging; an infra test now pins it.
- Destroying the registry destroys the images, so a fresh environment must push an
  image before its services can start. Documented in the runbook §0.

## Alternatives rejected

- **One stack for everything** - cannot be created from nothing, as above.
- **Two ALBs** - twice the fixed cost, no additional lesson at this level.
- **Host-based routing** - requires a domain and certificate the portfolio does not
  own yet (ADR 0004), and teaches nothing that port-based routing does not.
- **Blue/green (CodeDeploy)** - the correct end state for a production service, and
  explicitly a Level 3 topic; it needs a second target group pair and lifecycle
  hooks that would dominate this level's surface area.
