# Evidence: ALB access-log forensics (2026-10-17)

**What this is.** A read-back of the Level 1 ALB's own access logs, done *after*
[INC-0002](../incidents/0002-fault-detection-latency-and-unconfirmed-alert-path.md)
was written, to check the incident's published numbers against primary evidence.
It changed three things in the record and turned up hostile traffic that no other
artifact captured, so it is kept here as evidence rather than as a log excerpt.

**Source.** 23 gzipped objects (174 requests) under
`s3://incidenttriage-level1-albaccesslogsbucket9c39e353-voabyqrtbf56/AWSLogs/685134815483/elasticloadbalancing/us-east-1/2026/10/17/`,
which is the stack's `log_access_logs` bucket. Reproduce with
[`scripts/adhoc/alb_access_log_report.py`](../../scripts/adhoc/alb_access_log_report.py).

**Why it matters.** Access logs are the only place the *client-visible* truth
lives: they show what reached the load balancer, from whom, and what the ALB
returned. Alarm history shows what CloudWatch *thought*; metrics show what the
ALB *sampled*. This is the third, independent leg.

## 1. Health checks do not appear in these logs

The target group health-checks `/health` every 15 s (`interval=15`,
`healthy_threshold=2`, `unhealthy_threshold=3`). Over the ~2.5 h covered here that
is ~600 probes. **Not one line in the 174 entries carries a health-checker user
agent**, and no line hits `/health` at a 15 s cadence.

Consequence, and the reason this matters: **access logs cannot be used to measure
detection latency.** The health check is the detector that flips
`UnHealthyHostCount`, and its timing is invisible here. Any claim about *when the
ALB noticed* has to come from CloudWatch's metric and alarm history — which is
exactly the evidence INC-0002 leaned on. (This is a methodological limit, not a
bug: the number of logged lines equals the number of *client* requests
throughout.)

## 2. There were no 5xx responses during any of the three fault windows

The only server-error lines in the whole day are eight `502`s between
**14:44:45 and 14:46:32 UTC**, 15 s apart, `target=-`, user agent a PowerShell
client — i.e. **my own polling loop while the very first instance was still
booting** (the ALB existed and the listener was up before the app was listening;
the app first answered at ~14:46:4x). Every other request in the day returned
either `200` or `404`.

So during the v1/v2/v3 drills there is **no logged request at all** in the fault
windows (v1: no entries between 14:59 and 15:11; v3: only 200s at 15:35/15:38/
15:40/15:41/15:42). The honest reading: **no external client tried to use the API
during the drills, so there was no observed user-visible failure.** INC-0002's
"API requests failed from 15:03:57" is an *inference* from the service being
stopped, not an observation of a failed request. It should be stated as such.

## 3. Correction: the API was already serving before the recorded end of impact

INC-0002 recorded the v1 impact window as `15:03:57 → 15:12:56`. The access logs
disprove the end of that window:

```
15:11:49  elb=404 target=404  client=46.101.75.142  GET http://54.144.217.250:80/.git/config
```

`target=404` means **the application answered** — the ALB routed the request to a
listening app and the app returned its own 404. A stopped service would have
produced a `5xx` with `target=-`, not an application 404. So the service was back
up by **15:11:49 UTC**, i.e. at or before the recorded start-of-recovery
(`15:11:39`). The `15:12:56` in the incident was when I ran *my own* recovery
check, not when the service returned. Corrected impact window:
**15:03:57 → ~15:11:39** (~7m42s, and still no observed client failure).

## 4. New finding: the public ALB is being scanned by the internet

Everything below reached **the application** (`target=404`) and was refused only
because no such route exists. Nothing sensitive was served, but this is direct
evidence that the unauthenticated, cleartext, internet-facing surface described
in [RISK-1/RISK-2](../security-and-threat-model.md) is *actually being probed*,
not just theoretically exposed.

| When (UTC) | Source | User agent | What it asked for | Result |
| :--- | :--- | :--- | :--- | :--- |
| 14:55:38, 14:56:20 | 2 hosts | `zgrab/0.x` | `/developmentserver/metadata*` | 404 |
| 15:11:49 | 46.101.75.142 | spoofed Chrome-on-Linux | **`/.git/config`** (source-disclosure attempt) | 404 |
| 15:13:09–15:13:26 | 8 hosts | `Infrawatch/1.0` | `/`, `/favicon.ico`, **`/api/mcp`**, **`/mcp`**, `/mcp/`, **`/sse`** | 404 ×8 |
| 15:47:44, 16:31:45 | 45.198.224.124 | browser UA | `/` | 404 |
| 15:58:02, 16:24:34 | 2 hosts | iPhone UA | `/` | 404 |
| **16:10:03–16:10:17** | **82.64.18.167** | **`Go Web Pentest Tool`** | **128 requests** sweeping phpMyAdmin / MySQL-admin paths (`/phpMyAdmin-*/`, `/pma/`, `/mysql/…`, `/sql/…`) | **404 ×128** |
| 17:07:15 | 205.210.31.219 | `Hello from Palo Alto Networks, find out more about our scans` | `/` | 404 |

Three observations worth keeping:

- **The busiest minute of the day was an attack, not a user.** 128 of the day's 174
  logged requests (74%) are one scanner in ~15 seconds. An idle lab ALB does not
  stay unobserved on the internet.
- **The scanners are looking for AI/agent endpoints specifically.** `Infrawatch`
  probed `/api/mcp`, `/mcp`, `/mcp/` and `/sse` — Model Context Protocol server
  discovery. That this stack *is* an AI service makes that probing pointed rather
  than generic.
- **The app was reached, and that is the point.** Because it was reached, the
  requests also landed in the application log and in the `5xx`/request metrics —
  so this traffic is visible in three places, and the defence that kept it
  harmless (route-level 404s, no auth-free data routes) is a *default*, not a
  control. See RISK-11.

## 5. What I would not conclude from this

- **Not** "the app was attacked successfully." Every request 404'd; nothing was
  read, written or executed.
- **Not** that access logs can time detection — see §1.
- **Not** that the 502s were an incident: they are the expected cold-start of the
  first deploy, and they are the *only* place that cold start is recorded.

## 6. Reproduce

```bash
uv run --no-sync python scripts/adhoc/alb_access_log_report.py
# narrower window / characterise one scanner:
FOCUS_CLIENT=82.64.18.167 DAY=2026/10/17 uv run --no-sync python scripts/adhoc/alb_access_log_report.py
```

Read-only: a few `GetObject`s on a bucket holding kilobytes.
