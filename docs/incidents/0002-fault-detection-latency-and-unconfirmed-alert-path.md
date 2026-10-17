# INC-0002: Unhealthy target took 6m44s to page - and the page could not be delivered

- **Date / time (UTC):** 2026-10-17, 15:03:57-15:38:09
- **Environment:** Level 1 (`IncidentTriage-level1`, `us-east-1`)
- **Detected by:** deliberate fault injection (game day #1, SSM `systemctl stop`)
- **Severity / user impact:** API requests failed from 15:03:57 to 15:12:56
  (~9 minutes). No customers exist; the impact is the *detection* delay, not the
  outage.
- **Author:** repo owner (solo operator)

## Summary

Stopping the service did exactly what was expected to the ALB (target `unhealthy`
in ~78 seconds), but the alarm took **6m44s** to enter `ALARM`, and when it did,
the notification could not be delivered at all: the SNS email subscription is in
`PendingConfirmation`. The alarm is now tuned to fire on the first unhealthy
datapoint and the health check interval is halved; the same fault now pages in
**1m25s**.

## Timeline (UTC) - three iterations of the same fault

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
- Detection latency is now an asserted property of the stack (a test), not a
  hope - the same principle as the `/health` version check from INC-0001:
  **assert the outcome, not the configuration**.
- Level 2/3: replace email with a machine-consumable channel (SNS→Lambda→Slack/PagerDuty)
  whose subscription cannot be pending, and add an alarm on the *alerting path*
  itself (e.g. an alarm for zero ALARM-state transitions during a fault test).
- Owner: repo owner. Outstanding user action: **click the AWS confirmation email
  sent to `justin@zorneo.dev`**, otherwise every alarm in this stack is decorative.

## What I got wrong

- I read `target=unhealthy` as "the system detected the fault" and nearly moved on
  when the alarm had not fired within five minutes; the correct read was that the
  ALB *is* the detector and the alarm is the *notifier*, and those are separately
  configurable failure points.
- I first assumed the slow alarm was a dimension mismatch (a classic cause). The
  arithmetic of the two debounce stages explained the observed delay exactly, which
  is why measurement - not intuition - drove the fix.
