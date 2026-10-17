# Incident Report Template

Copy this file to `docs/incidents/NNNN-short-title.md` for each incident.
First-hand evidence only: paste real command output, metric values, and log
lines. If a number is not measured, say "not measured" instead of estimating.

---

# INC-XXXX: <one-line symptom>

- **Date / time (UTC):**
- **Environment:** Level 1 (`IncidentTriage-level1`, `us-east-1`)
- **Detected by:** (alarm name / human / smoke test)
- **Severity / user impact:** e.g. "all POST /incidents returned 202 for 12 min"
- **Author:**

## Summary

Two or three sentences: what broke, for how long, what fixed it.

## Timeline (UTC)

| Time | Event |
| :--- | :--- |
| HH:MM | Change deployed / condition introduced |
| HH:MM | First signal (metric, alarm, log line) |
| HH:MM | Alert delivered / detected |
| HH:MM | Investigation started |
| HH:MM | Root cause identified |
| HH:MM | Mitigation applied |
| HH:MM | Service confirmed healthy |

## Detection

- Which alarm or query fired? Paste the alarm definition / query.
- **MTTD** (condition → detected):
- **MTTR** (detected → healthy):

## Symptom

Exact observable behaviour. Commands and their real output:

```bash
# e.g.
curl -s https://<alb>/health
aws elbv2 describe-target-health --target-group-arn <arn>
```

## Investigation

- **Hypothesis 1:** ... → evidence for/against
- **Hypothesis 2:** ... → evidence for/against
- Commands / metrics / log queries that narrowed it (paste outputs):
  ```
  fields @timestamp, request_id, status, error | filter level="error"
  ```

## Root cause

The specific, falsifiable cause (a bad ARN, a stopped service, a missing route,
a wrong env var), not "human error".

## Fix

What was changed, and the exact command/commit.

## Prevention / permanent corrective action

- What stops this class of failure, or makes it detectable sooner?
- Owner and target date.

## What I got wrong

The wrong assumption from the investigation, and the evidence that corrected it.
This is the most valuable section - do not skip it.
