# ADR 0009: GitHub OIDC deploy role that owns the artifact, not the infrastructure

- **Status:** Accepted (Level 2)
- **Date:** 2026-10-18

## Context

The pipeline must be able to release a new version without anyone holding AWS
credentials in GitHub. That means an IAM role assumed via OIDC. The question is how
much that role should be able to do - and it is a real fork in the road, because the
easy answer is "let it run `cdk deploy`".

## Decision

The deploy role may **push an image and move an ECS service between immutable task
definition revisions**. It may not run CloudFormation.

```python
# scripts/deploy_ecs_image.py - what the pipeline actually does
previous = ecs.describe_task_definition(taskDefinition=family)["taskDefinition"]
payload   = build_registration(previous, image_uri, image_tag)   # image + GIT_SHA
new       = ecs.register_task_definition(**payload)["taskDefinition"]
ecs.update_service(cluster=..., service=..., taskDefinition=new["taskDefinitionArn"],
                   forceNewDeployment=True)
```

CloudFormation (via CDK, run by a human) owns the task definition's **shape**:
cpu/memory, the two roles, the injected secret, log configuration, the health
check. The pipeline owns the **artifact**: which immutable tag the service runs.

## Rationale

- **A deploy role that can run CloudFormation can change anything in the stack**,
  including IAM. Scoping it to ECS + ECR means a compromised pipeline cannot widen
  its own permissions, cannot create a user, and cannot touch the database. The
  pipeline is the most exposed part of the system (it runs code from a pull
  request), so it gets the narrowest credential that still delivers.
- **The subject is pinned, not wildcarded.** The trust policy allows exactly two
  subjects: a push to the deploy branch, and the protected `production`
  Environment. GitHub's immutable subject form (`owner@ownerId/repo@repoId`) is
  used, which is stricter than names and survives renames. Getting this wrong is
  the documented way OIDC setups are exploited - and getting it *right* is subtle
  enough that it broke twice here (see
  [`docs/evidence/level2-oidc-subject.md`](../evidence/level2-oidc-subject.md)).
- **`iam:PassRole` is the real blast radius**, so it is limited to exactly four
  role ARNs (two task roles, two execution roles) and conditioned on
  `iam:PassedToService = ecs-tasks.amazonaws.com`.
- **The retry loop is free.** Registering a revision and moving a service is
  idempotent-ish and fast, so a failed deploy can be retried without a
  CloudFormation update noise cycle.

## Consequences

- The image tag is context-driven (`-c imageTag=<sha>`). A manual `cdk deploy`
  without it will point services at whatever the local git HEAD is, which may not
  be in ECR. `scripts/deploy_ecs_image.py` now refuses to start if the tag is
  missing, and lists the tags that do exist.
- The task definition exists in two places conceptually - the template and the
  running revision - so the rule is: **structural change goes through CDK; a new
  image goes through the pipeline.** A `cdk deploy` will reset the image tag to its
  context value, which is documented in the runbook rather than left as a surprise.
- Rollback is the same command aimed at an older tag, so there is no separate
  rollback procedure to keep in sync.

## Alternatives rejected

- **`cdk deploy` from the pipeline** - one source of truth, but requires
  CloudFormation plus broad create/update permissions on every resource type in the
  stack, and `iam:PassRole` for any role the stack defines. A much larger blast
  radius for a smaller conceptual gain; it would also make the pipeline the only
  thing that can deploy, which is worse for a portfolio that must be reproducible
  by hand.
- **Long-lived IAM user + access key in GitHub secrets** - exactly the practice
  this project is meant to demonstrate avoiding, and nothing about it is
  short-lived or revocable.
- **A wildcard `sub` (`repo:owner/repo:*`)** - simpler, and the specific mistake
  AWS warns about; it would let any branch, pull request, or environment assume
  the deploy role.
