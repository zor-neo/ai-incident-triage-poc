# INC-0001: `cdk deploy` reported success while the app kept serving the previous commit

- **Date / time (UTC):** 2026-10-17, ~14:50-14:59
- **Environment:** Level 1 (`IncidentTriage-level1`, `us-east-1`)
- **Detected by:** human (post-deploy version check on `GET /health`)
- **Severity / user impact:** none observable. The API stayed healthy and served
  correct triage results on the **old** code for ~8 minutes. The risk is silent:
  a bug fix deploy can report success and ship nothing.
- **Author:** repo owner (solo operator)

## Summary

A sanitizer fix was committed (`1d06863`) and deployed with `cdk deploy`. CloudFormation
reported `UPDATE_COMPLETE` and `cdk` printed `✨ Total time: 122.82s`. The running
application was still the previous build (`version=76ad3dd`), because the instance
was only **restarted in place** and its user-data bootstrap never re-ran. The
stack was fixed to replace the instance when user data changes (`5e04ba0`), which
was verified live.

## Timeline (UTC)

| Time | Event |
| :--- | :--- |
| 14:50:11 | Stack `UPDATE_IN_PROGRESS` (`cdk deploy`, sanitizer fix `1d06863`) |
| 14:50:16 | `AppInstance` `UPDATE_IN_PROGRESS` |
| 14:50:37 | Instance `LaunchTime` moves - CloudFormation restarted it (stop/start) |
| 14:50:50 | `AppInstance` `UPDATE_COMPLETE` |
| 14:50:52 | Stack `UPDATE_COMPLETE`; `cdk` reports success |
| ~14:51 | **Detected:** `GET /health` still returns `version=76ad3dd` (stale) |
| ~14:52 | Same `InstanceId`, `LaunchTime` changed, `UserData` attribute updated |
| ~14:53 | **Root cause identified** (CFN reference: user-data update = restart, and user data runs once per boot) |
| 14:57:07 | Stack `UPDATE_IN_PROGRESS` (fix `5e04ba0`, `user_data_causes_replacement=True`) |
| 14:57:13 | New instance launched (`i-086504b397af2e077`) |
| 14:58:54 | Old instance `DELETE_IN_PROGRESS` (drained, then terminated) |
| 14:59:17 | Stack `UPDATE_COMPLETE` |
| ~14:59 | **Verified:** `GET /health` returns `version=5e04ba0`; redaction fix live |

## Detection

Not caught by an alarm - there is no alarm for "the running build is older than
the deployed commit". It was caught by a manual verification step that compares
`/health`'s `version` (the git SHA baked into user data) against `git rev-parse HEAD`.

- **MTTD** (change "successful" → detected): ~1 minute, but only because the
  version was checked by hand. An unattended deploy would have gone unnoticed.
- **MTTR** (detected → replacement instance serving traffic): ~8 minutes
  (14:51 → 14:59:17). Approximated from CloudFormation event timestamps, not
  stopwatch-measured.

## Symptom

`cdk deploy` succeeded, target group stayed `healthy` throughout, and the API
answered - with old code:

```bash
$ curl -s "$BASE_URL/health"
{"status":"healthy","version":"76ad3dd","environment":"level1"}
# expected: 1d06863 (the commit that was just deployed)
```

CloudFormation had, in fact, done exactly what it was told - update the instance:

```
| AppInstance13C82D50 | UPDATE_IN_PROGRESS  | 2026-10-17T14:50:16.420000+00:00 |
| AppInstance13C82D50 | UPDATE_COMPLETE     | 2026-10-17T14:50:50.112000+00:00 |
| IncidentTriage-level1 | UPDATE_COMPLETE   | 2026-10-17T14:50:52.865000+00:00 |
```

```bash
$ aws ec2 describe-instances --instance-ids i-06425bd3f7445dcd6 \
    --query "Reservations[].Instances[].{LaunchTime:LaunchTime,State:State.Name}"
[{"LaunchTime": "2026-10-17T14:50:37+00:00", "State": "running"}]
# same InstanceId as before the deploy; LaunchTime moved into the update window
```

## Investigation

- **Hypothesis 1: the deploy never published the new asset.** Refuted - the stack
  updated `AppInstance` at 14:50:16, which only happens when the rendered user data
  (which embeds the content-hashed asset URL and the git SHA) changed.
- **Hypothesis 2: the instance was replaced and the new one failed to bootstrap.**
  Refuted - `InstanceId` was unchanged and the application was healthy.
- **Hypothesis 3: the instance was restarted, so the service came back with new
  code.** Refuted - `/health` still reported `76ad3dd`.
- **Hypothesis 4: user data changed but was never executed.** Confirmed. The
  CloudFormation reference for `AWS::EC2::Instance` `UserData` says: *"If the root
  volume is an EBS volume and you update user data, CloudFormation restarts the
  instance"* (`Update requires: Some interruptions`). Cloud-init runs user-data
  scripts once per instance, not once per boot, so the restart wrote the new
  script and never ran it. The app process was never re-executed with the new code
  or the new `GIT_SHA`, so it kept serving the old build with the old environment file.

## Root cause

The instance was created with CDK's default `user_data_causes_replacement=False`,
matching CloudFormation's semantics: a user-data change is an **in-place restart**.
Because the application code is delivered *through user data* (S3 asset URL + boot
script), an in-place restart is a no-op for the application. CloudFormation
reported success because the resource really did reach the requested state - the
requested state simply does not include "run the new code".

## Fix

Force replacement of the instance when user data changes, so the bootstrap script
actually executes:

```python
# infra/stacks/level1_ec2_stack.py
app_instance = ec2.Instance(
    ...,
    require_imdsv2=True,
    user_data_causes_replacement=True,
    ...,
)
```

Commit `5e04ba0`. Verified by change set before deploying, then live:

```
$ cdk diff -c gitSha=probe-replacement-check
[-] AWS::EC2::Instance AppInstance AppInstance13C82D50 destroy
[+] AWS::EC2::Instance AppInstance AppInstance13C82D507c2b21d2103f83c1
```

```
$ curl -s "$BASE_URL/health"
{"status":"healthy","version":"5e04ba0","environment":"level1"}
```

## Prevention / permanent corrective action

- `cdk deploy` is not "done" until `GET /health` reports the deployed commit - now
  a mandatory step in `docs/runbooks/level1-runbook.md` §2 and in
  `scripts/smoke_test.sh`. Owner: repo owner. Done.
- ADR `0006-replace-instance-on-user-data-change.md` records the decision and its
  cost (a short, ALB-drained instance replacement per application change).
- The lesson is structural: **never infer deployment success from a control-plane
  status** (`UPDATE_COMPLETE`). Assert on the data plane (a version endpoint or a
  smoke test). Level 2 removes the class of problem by shipping an immutable
  container image to ECS, where "new task definition" and "code" cannot drift.

## What I got wrong

- I assumed "`cdk deploy` succeeded" ⇒ "the running code changed". The corrective
  evidence was `/health` reporting the previous SHA while the stack was green.
- I also briefly suspected the asset had not been rebuilt, because the LaunchTemplate
  in the stack looked unattached. The decisive fact was not the launch template at
  all, but that CloudFormation's documented `UserData` behavior is "restart", and
  restarting an instance does not re-run cloud-init user data.
