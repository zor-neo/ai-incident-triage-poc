# INC-0004: A bad release was promoted to staging - and staging never noticed

- **Date / time (UTC):** 2026-10-18, 19:43-19:55 (game day, deliberate)
- **Environment:** Level 2 staging (`incident-triage-staging`, listener `:8080`)
- **Detected by:** the ALB unhealthy-targets alarm, then the deploy script's own
  failure
- **Severity / user impact:** **zero** - staging served the previous healthy
  version throughout. The impact is the *release* signal, not the service.
- **Author:** repo owner (solo operator)

## Summary

A deliberately broken image (identical application bytes, but the process never
serves, so no health check can pass) was registered as a new task definition
revision and rolled onto the staging service. The result was the behaviour the
design intends:

1. ECS started the new task and kept the old one running, because
   `min_healthy_percent: 100` - **staging never stopped serving**.
2. The new task registered with the target group and failed its health check.
3. The unhealthy-targets alarm fired.
4. ECS killed the unhealthy task itself (`Task failed ELB health checks`).
5. The deployment never reached a steady state, so the deploy command failed -
   which is the signal a human acts on.
6. Rollback was redeploying the previous immutable tag, which took the service
   from revision 7 to revision 8 and completed normally.

## Evidence

**Staging kept serving the old version** while the bad revision was live:

```
$ curl http://<alb>:8080/health
{"status":"healthy","version":"331dfbe","environment":"staging"}
```

`331dfbe` is the previous good release, and it never changed during the incident.

**Two deployments coexisted** - the old one deliberately kept at 1 running task
with `desired: 0`:

```
incident-triage-staging deployments:
  PRIMARY  IN_PROGRESS  desired=1 running=1 failed=1  taskDef=...:7   <- the bad release
  ACTIVE   COMPLETED    desired=0 running=1 failed=0  taskDef=...:6   <- still serving
```

**The alarm fired** (note the ~3 minute lag, consistent with INC-0002):

```
IncidentTriage-level2-staging-alb-unhealthy-targets
  StateValue: ALARM
  Reason: Threshold Crossed: 1 out of the last 1 datapoints [1.0 (17/10/26
          19:44:00)] was greater than or equal to the threshold (1.0)
  History: OK -> ALARM  2026-10-18T19:47:09Z
```

**ECS removed the bad task on its own:**

```
stopCode: ServiceSchedulerInitiated
health:   UNHEALTHY
reason:   Task failed ELB health checks in (target-group
          arn:aws:elasticloadbalancing:us-east-1:685134815483:targetgroup/
          incident-triage-staging/0ef2b3a786dbf4de)
```

**The deploy command reported failure rather than pretending success** - it polled
`rolloutState` and never saw `COMPLETED`:

```
  rollout=IN_PROGRESS running=0/1 pending=0 failed=1   (x14)
FAIL: incident-triage-staging deployment ended as TIMEOUT
```

**Rollback = redeploy the previous immutable tag:**

```
deploying incident-triage-staging
  from revision 7 -> .../incident-triage:331dfbe
  registered revision 8
  rollout=COMPLETED running=1/1 pending=0 failed=0
PASS: incident-triage-staging is serving 331dfbe
```

## What this demonstrates

- **Rolling deployment with `min_healthy_percent: 100` is a genuine availability
  control, not a formality.** The service was never degraded, because ECS refuses
  to drain healthy tasks until replacements are healthy. A bad release costs a
  *failed deployment*, not an outage.
- **Detection is separate from protection.** Protection (zero downtime) was
  automatic; detection still needed the alarm, and the alarm still took ~3 minutes
  to become visible - the same CloudWatch pipeline lag measured in INC-0002.
- **A failed deployment is a first-class signal.** Because the deploy command
  exits non-zero, the pipeline stops and no human has to notice the alarm for the
  release to be halted.

## What it does not demonstrate

- **No automatic rollback.** This level has the circuit breaker *enabled but not
  rolling back* (`DeploymentCircuitBreaker(rollback=False)`), so ECS failed the
  deployment and left the decision to a human, who redeployed the previous tag.
  Automatic rollback on a CloudWatch deployment alarm is a Level 3 control - and
  this incident is the evidence for why it is worth having: the human step is
  correct but it is a step.
- **No blue/green, no canary.** A rolling deployment protected availability but a
  canary would have caught "the new version never becomes healthy" before an
  entire new task was scheduled.

## Prevention / permanent corrective action

- **Stage first, and verify with the real contract.** The pipeline's staging job
  runs `scripts/smoke_test.sh` against the live staging URL; a bad release that
  *starts* but misbehaves fails there, before the production gate.
- **Rollback is a command, not a procedure:** `scripts/deploy_ecs_image.py
  --service <svc> --image-tag <previous>`. Documented in
  [`docs/runbooks/level2-runbook.md`](../runbooks/level2-runbook.md) §4.
- Level 3: enable the circuit breaker's rollback and attach CloudWatch deployment
  alarms so steps 5 and 6 above happen without a human.
- Owner: repo owner. This was a deliberate exercise (game day), not an accident;
  the "bad release" image is built by `scripts/adhoc/rollback_lab.sh` and is never
  promoted to production.
