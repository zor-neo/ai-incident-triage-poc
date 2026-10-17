# ADR 0006: Replace the instance when user data changes

- **Status:** Accepted (Level 1; Level 2 ships immutable images instead)
- **Date:** 2026-10-17

## Context

Level 1 delivers the application through EC2 user data: the bootstrap script
downloads the CDK S3 asset (a content-hashed `requirements.lock.txt` + `app/`
bundle), builds a venv, and writes `/etc/incident-triage.env` including the git
SHA served by `GET /health`.

CloudFormation's documented behavior for `AWS::EC2::Instance` `UserData` is
`Update requires: Some interruptions`: *"If the root volume is an EBS volume and
you update user data, CloudFormation restarts the instance."* Cloud-init executes
user-data scripts **once per instance**, not once per boot.

Combined, those two facts produce a silent failure: a `cdk deploy` that changes the
code restarts the instance, writes the new script, never runs it, and still reports
`UPDATE_COMPLETE` with the target group green. This happened in production during
this session - see `docs/incidents/0001-stale-code-after-successful-deploy.md`.

Options considered:

1. Leave the default (`user_data_causes_replacement=False`) and treat the deploy as
   "someone must re-run the bootstrap by hand".
2. Set `user_data_causes_replacement=True` so a user-data change **replaces** the
   instance.
3. Move the code-delivery mechanism out of user data (e.g. an SSM document, a
   golden AMI, CodeDeploy, or containers on ECS).

## Decision

Set **`user_data_causes_replacement=True`** on `AppInstance` (commit `5e04ba0`).

## Rationale

- It restores the invariant operators expect: *changed code ⇒ the running process
  is running that code*. "Deploy" and "restart" stop being different things.
- The replacement is safe by construction here: the ALB drains the old target
  (`deregistration_delay=15s`) and only the new instance registers after its
  `/health` check passes. Bootstrapping is idempotent, so a rollback is just
  deploying the previous SHA.
- It is the smallest change that fixes the class of bug at this level, and it keeps
  Level 1's cost model (~$0.09/hour, one `t4g.small`).

## Consequences

- **Every application change now costs a full instance replacement** (measured:
  ~2 minutes from `cdk deploy` to a healthy replacement target). Briefly there is
  exactly one target and it is bootstrapping, so a single-instance Level 1 has a
  short window where capacity is reduced. Level 1 is a lab; this is accepted and
  documented rather than engineered away.
- Rollback semantics stay simple and honest: the previous SHA is a redeploy, not a
  restore of a stopped instance.
- **Every commit now replaces the instance**, not just application commits. The
  deployed commit SHA is embedded in user data (it is the `version` served by
  `/health`), so a docs-only or alarm-only commit changes user data and triggers a
  replacement too. Cost of a replacement is ~2 minutes; the instance id and launch
  time change on every deploy. This is accepted: it makes "which build is running"
  unambiguous at the cost of churn. A finer-grained alternative (version the
  instance by the application *asset* hash only, and report the SHA from a separate
  mechanism) is deliberately not taken at Level 1 - it would weaken the guarantee
  that `/health` names the exact deployed commit.
- Infra-only resources that are not part of user data (alarms, buckets, IAM, the
  target group) still update in place; the instance is the only resource that
  churns.
- `docs/runbooks/level1-runbook.md` §2 makes `GET /health` version verification a
  mandatory post-deploy step, because the version endpoint - not `cdk`'s exit
  code - is the evidence that a deploy shipped.

## Alternatives rejected

- **Default in-place behavior + manual bootstrap.** Keeps a documented footgun and
  makes deploys unauditable; the failure is invisible until someone diffs versions.
- **SSM Document / CodeDeploy / golden AMI.** Real options, but each adds a new
  mechanism to learn and secure for a single-instance lab. They are **Level 2's
  answer** in a better form: an immutable container image in ECR deployed to ECS
  Fargate, where "which code is running" is a task-definition attribute and can
  never silently drift from what was deployed.
