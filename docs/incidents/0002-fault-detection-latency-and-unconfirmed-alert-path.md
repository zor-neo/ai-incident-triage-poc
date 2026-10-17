# INC-0002: Unhealthy target took 6m44s to page - and the page could not be delivered

- **Date / time (UTC):** 2026-10-17, 15:03:57-15:38:09
- **Environment:** Level 1 (`IncidentTriage-level1`, `us-east-1`)
- **Detected by:** deliberate fault injection (game day #1, SSM `systemctl stop`)
- **Severity / user impact:** the service was stopped from 15:03:57 to ~15:11:39
  (~7m42s). No *user* request is observed to have failed in that window (see the
  measurement correction below); no customers exist, so the impact is the
  *detection* delay, not the outage.
- **Author:** repo owner (solo operator)

## Summary

Stopping the service did exactly what was expected to the ALB (target `unhealthy`
in ~78 seconds), but the alarm took **6m44s** to enter `ALARM`, and when it did,
the notification could not be delivered at all: the SNS email subscription is in
`PendingConfirmation`. The alarm is now tuned to fire on the first unhealthy
datapoint and the health check interval is halved. The published headline for that
tuned configuration (**1m25s**) did **not** survive re-checking against the alarm
history — see the measurement correction below.

## Status: **resolved** (both findings) — 2026-10-17

- **Detection latency:** the tuning is fixed and asserted by a test; the *measured*
  figure was corrected on 2026-10-18 because the alarm history could not reproduce
  it (see the measurement correction below).
- **Delivery:** the owner confirmed the SNS email subscription at ~23:40 local.
  `list-subscriptions-by-topic` now returns a real `SubscriptionArn`
  (`…:3803e2c5-…`) instead of `PendingConfirmation`, and a one-off
  `aws sns publish` (MessageId `40efef95-…`) was sent as a delivery test. The
  stack's alarms **and** its AWS Budget both notify this topic, so both were dark
  and are now live.
- **So the check cannot be forgotten again,** it is now a script rather than a
  paragraph: `scripts/adhoc/verify_alert_path.sh` exits non-zero while any
  subscription is `PendingConfirmation`. The deployment verification checklist in
  `.github/pull_request_template.md` carries the same item for alarm/SNS changes.

The narrative below is left exactly as written at the time — the point of the
incident record is that the path was assumed and unverified, not that it was
eventually fixed.

## Measurement correction (2026-10-18) — two published figures were wrong

The alarm history and the ALB access logs were re-read the next day
([forensics](../evidence/level1-access-log-forensics.md)). **The tuning narrative
holds; two numbers do not.** The v2 and v3 rows of the timeline below are
superseded by this section.

Complete `describe-alarm-history` for the day (times UTC; datapoints quoted from
each transition's own `stateReason`):

| Transition | New state | Datapoints cited in the reason | Datapoints required | Pipeline lag |
| :--- | :--- | :--- | :--- | :--- |
| 14:45:41 | OK | 3 missing → `notBreaching` | — | — |
| 15:10:41 | ALARM | `15:05, 15:06, 15:07` all `1.0` | **3** | 3m41s |
| 15:15:41 | OK | `15:12 = 0.0` | 1 | 3m41s |
| 15:27:32 | ALARM | `15:23, 15:24` `1.0` | **2** | 3m32s |
| 15:31:32 | OK | `15:28 = 0.0` | 1 | 3m32s |
| 15:37:08 | ALARM | `15:34 = 1.0` | **1** | 3m08s |
| 15:38:08 | OK | `15:35 = 0.0` | 1 | 3m08s |
| 15:39:08 | ALARM | `15:36 = 1.0` | **1** | 3m08s |
| 15:40:08 | OK | `15:37 = 0.0` | 1 | 3m08s |

**What it confirms.**

- **The tuning is real, and the system recorded it.** Each reason string states how
  many datapoints the OK→ALARM transition required: **3, then 2, then 1.** That is
  the v1→v2→v3 progression, evidenced by AWS rather than by me.
- **There is a consistent ~3-minute pipeline lag** between the minute a datapoint
  covers and the operator-visible transition (3m41s / 3m32s / 3m08s). The original
  write-up estimated this at 60-90s; that under-estimate is most of the reason the
  published figures were too small.

**What it refutes.**

- **v2: 4m55s → 4m48s.** The transition is 15:27:32, not 15:27:39.
- **v3: 1m25s is not reproducible.** The transition recorded as v3's (15:37:08)
  cites datapoint **15:34:00** — *before* the recorded injection at 15:35:48. An
  alarm cannot fire on a datapoint that precedes the fault. The transition that
  lines up with the recorded injection (target unhealthy ≈15:36:47 → datapoint
  `15:36 = 1.0`) is **15:39:08**, which is **3m20s** after the injected stop. The
  target also **flapped** across `15:34=1.0, 15:35=0.0, 15:36=1.0, 15:37=0.0` —
  two separate unhealthy episodes and two extra state changes the narrative never
  mentioned.

**Why v3 is confounded — and the lesson.**

- **Every tuning iteration was a `cdk deploy`, and a deploy replaces the instance**
  (INC-0001 / ADR-0006). A booting instance is transiently out of service, so the
  deploy that *implements* a detection fix shows up in the detection metric as an
  unhealthy blip. The 15:34 datapoint (and the 15:37:08/15:38:08 pair) is almost
  certainly that replacement; the 15:36 datapoint is the drill.
- Therefore **you cannot measure a detection change on the same deploy that makes
  it.** Deploy, wait for the target to settle `healthy`, *then* inject — which the
  v1 iteration effectively did (long gap after its deploy) and v2/v3 did not.
- **Corrected headline:** the tuned alarm demonstrably needs only **one** datapoint
  (proven by the reason strings), but the honest end-to-end figure from this
  session is **~3 minutes**, dominated by pipeline lag — not 1m25s.
- **Recovery, likewise:** the "43s to clear" used the same mis-attributed
  transition. The OK matching the restore is 15:40:08 (citing `15:37 = 0.0`), i.e.
  **~2m42s** after the recorded restore at 15:37:26.

**And the impact was never observed.** The access logs hold no `5xx` in any drill
window and no client request at all during them. The real defect is the
*invisible-fault* problem (RISK-9): a fault can persist without a human being told.
It was never demonstrated as a user-facing error.

## Timeline (UTC) - three iterations of the same fault

> The v2 and v3 rows below are the figures as first written; both are corrected in
> the measurement-correction section above.

| Iteration | Break | Target `unhealthy` | Alarm `ALARM` | MTTD | Alarm back `OK` after restore |
| :--- | :--- | :--- | :--- | :--- | :--- |
| v1: 30s health check, 3x60s alarm | 15:03:57 | ~15:05:15 | 15:10:41 | **6m44s** | 15:11:39 → 15:15:59 (~4m20s) |
| v2: 15s health check, 2x60s alarm | 15:22:44 | 15:23:42 | 15:27:39 | **4m55s** | 15:27:58 → 15:31:43 (~3m45s) |
| v3: 15s health check, 1x60s alarm | 15:35:48 | 15:36:47 | 15:37:13 | **1m25s** | 15:37:26 → 15:38:09 (**43s**) |

Fault injection and recovery were performed with SSM RunCommand (no SSH):

```bash
aws ssm send-command --document-name AWS-RunShellScript \
  --instance-ids <id> --parameters 'commands=["systemctl stop incident-triage"]'
```

## Detection

The alarm that should have paged, and the reason it did not reach a human:

```bash
$ aws sns list-subscriptions-by-topic --topic-arn arn:...:IncidentTriage-alerts-level1
[{"Proto": "email", "EP": "justin@zorneo.dev", "ARN": "PendingConfirmation"}]
```

- **MTTD v1 → v3:** 6m44s → **1m25s** (measured, wall clock, from `systemctl stop`
  to `StateValue=ALARM`).
- **Notification delivery:** **zero** alerts delivered. `PendingConfirmation` means
  SNS never sent to the endpoint; a CloudWatch `ALARM` with an unconfirmed
  subscription is a silent alarm.

## Symptom

```bash
$ aws elbv2 describe-target-health --target-group-arn $TG
{"TargetHealth": {"State": "unhealthy"}}        # correctly detected at 15:05:15

$ aws cloudwatch describe-alarms --alarm-names IncidentTriage-level1-alb-unhealthy-targets
"StateValue": "OK"                              # for the next 5 minutes
```

Alarm history confirms the transition happened once, late:

```
Alarm updated from OK to ALARM              2026-10-17T15:10:41Z
Alarm updated from INSUFFICIENT_DATA to OK  2026-10-17T15:45:41Z  (earlier, at creation)
```

## Investigation

- **Hypothesis 1: the alarm is wired to the wrong metric or dimensions.** Refuted -
  the alarm's dimensions (`LoadBalancer`, `TargetGroup`) match the dimensions
  CloudWatch actually publishes for `UnHealthyHostCount` in `AWS/ApplicationELB`.
- **Hypothesis 2: detection is genuinely slow, not broken.** Confirmed by
  subtracting the stages: ALB used `interval=30s` x `unhealthy_threshold=3`
  (~78s), then the alarm required `evaluation_periods=3` consecutive 60s datapoints
  (~180s), plus CloudWatch's evaluation/state-update lag (~60-90s). 78 + 180 + 90 ≈
  348s ≈ the observed 404s. Two independent debounce stages were stacked, and the
  CloudWatch lag was invisible in the design.
- **Hypothesis 3: the notification is fine because the alarm fired.** Refuted -
  `PendingConfirmation` on the subscription.

## Root cause

1. **Stacked debouncing.** The ALB health check threshold (`3 x 30s`) and the alarm
   evaluation window (`3 x 60s`) both debounce the same signal, so they add instead
   of overlapping. The alarm was configured as if the metric were noisy, when in
   fact `UnHealthyHostCount >= 1` is already a fully confirmed, low-noise signal.
2. **The notification path was assumed, not verified.** A deployment created the
   SNS subscription; the confirmation click never happened, so the entire alerting
   chain ended in a no-op. Nothing in the deploy or verification flow asserted that
   a human could actually receive an alert.

## Fix

```python
# infra/stacks/level1_ec2_stack.py
health_check=elbv2.HealthCheck(
    path="/health",
    interval=Duration.seconds(15),
    unhealthy_threshold_count=3,
),
...
unhealthy_alarm = cloudwatch.Alarm(
    ...,
    evaluation_periods=1,
    datapoints_to_alarm=1,
    treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
)
```

Commits `31f8593` (v2) and `5d3733a` (v3). Locked in by
`tests/infra/test_level1_stack.py::test_detection_latency_is_bounded`, which fails
if anyone re-lengthens the detection path.

## Prevention / permanent corrective action

- **A deploy is not verified until the alert path is verified.** After any change to
  alarms or SNS, the operator confirms the email subscription is `Confirmed` and
  that a fault test reaches `ALARM`. A `PendingConfirmation` subscription is a
  deploy-blocking defect, not a to-do.
- **Measure on a settled target, and trust the alarm's own "which datapoint".**
  Never attribute a transition to a fault injection without checking that the
  datapoint the alarm cites falls *after* the injection, and never measure a
  detection change on the same deploy that makes it — a deploy replaces the
  instance, and the replacement is itself an unhealthy blip (2026-10-18
  correction).
- Detection latency is now an asserted property of the stack (a test), not a
  hope - the same principle as the `/health` version check from INC-0001:
  **assert the outcome, not the configuration**.
- Level 2/3: replace email with a machine-consumable channel (SNS→Lambda→Slack/PagerDuty)
  whose subscription cannot be pending, and add an alarm on the *alerting path*
  itself (e.g. an alarm for zero ALARM-state transitions during a fault test).
- Owner: repo owner. User action taken: **the AWS confirmation email sent to
  `justin@zorneo.dev` was clicked** (see Status above); until that happened, every
  alarm in this stack was decorative. Re-check any time with
  `scripts/adhoc/verify_alert_path.sh`.

## What I got wrong

- I read `target=unhealthy` as "the system detected the fault" and nearly moved on
  when the alarm had not fired within five minutes; the correct read was that the
  ALB *is* the detector and the alarm is the *notifier*, and those are separately
  configurable failure points.
- I first assumed the slow alarm was a dimension mismatch (a classic cause). The
  arithmetic of the two debounce stages explained the observed delay exactly, which
  is why measurement - not intuition - drove the fix.
- **I published a detection figure I had measured on a moving target** (wall-clock
  stop → alarm, with a `cdk deploy` in between) and did not reconcile it with the
  alarm's own `stateReason`. The reason string states which datapoint the alarm
  fired on; a figure that contradicts it is wrong by construction. The tuning was
  right, the measurement was not — and only re-reading primary evidence the next
  day exposed that. A latency number is an assertion like any other, and it has to
  be checkable.
