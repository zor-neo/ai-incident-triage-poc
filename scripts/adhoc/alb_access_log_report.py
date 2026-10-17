#!/usr/bin/env python
# ---------------------------------------------------------------------------
# scripts/adhoc/alb_access_log_report.py — read the ALB access logs out of S3
#
# WHEN THIS WAS USED
#   2026-10-18 (a few hours after the drills), to re-check INC-0002's published
#   timings against primary evidence. It found three things the incident had
#   wrong or missing:
#     * the API was already serving at 15:11:49 (a scanner got a 404 from the
#       app), so the documented impact window ended earlier than recorded;
#     * alarm transitions are visible ~3 minutes after the datapoint their own
#       StateReason cites, which the incident had guessed at 60-90s;
#     * **health-check requests are not in these logs at all**, so they cannot be
#       used to measure detection latency - only CloudWatch's metrics and the
#       alarm history can.
#   It also turned up hostile traffic: a phpMyAdmin brute-force (128 requests in
#   15s), MCP/SSE discovery, and .git/config probes against the public ALB.
#
# WHAT IT DOES
#   Lists every object under the ALB access-log prefix, gunzips them, parses the
#   standard ALB access-log fields, and prints:
#     1. a per-minute request/failure table,
#     2. every non-2xx line with client, error reason, user agent and path,
#     3. a user-agent census (scanners are obvious here),
#     4. the top requested paths for one client, to characterise a scan.
#
# HOW TO RUN
#   uv run --no-sync python scripts/adhoc/alb_access_log_report.py
#   BUCKET=... DAY=2026/10/17 uv run --no-sync python scripts/adhoc/alb_access_log_report.py
#
# COST: a few GETs on a bucket holding kilobytes. Read-only.
# ---------------------------------------------------------------------------
from __future__ import annotations

import gzip
import io
import os
from collections import defaultdict

import boto3

BUCKET = os.environ.get("BUCKET", "incidenttriage-level1-albaccesslogsbucket9c39e353-voabyqrtbf56")
DAY = os.environ.get("DAY", "2026/10/17")
FOCUS_CLIENT = os.environ.get("FOCUS_CLIENT", "")

FIELDS = [
    "type",
    "time",
    "elb",
    "client",
    "target",
    "req_proc",
    "tgt_proc",
    "resp_proc",
    "elb_status",
    "target_status",
    "recv_bytes",
    "sent_bytes",
    "request",
    "user_agent",
    "ssl_cipher",
    "ssl_proto",
    "tg_arn",
    "trace_id",
    "domain",
    "cert",
    "rule_prio",
    "req_creation",
    "actions",
    "redirect",
    "error_reason",
    "target_list",
]


def split_line(line: str) -> list[str]:
    """ALB access log fields are space separated with quoted strings inside."""
    out, cur, in_q = [], "", False
    for ch in line:
        if ch == '"':
            in_q = not in_q
        elif ch == " " and not in_q:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def load_rows() -> list[dict]:
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    account = os.environ.get("ACCOUNT", "685134815483")
    prefix = f"AWSLogs/{account}/elasticloadbalancing/us-east-1/{DAY}/"
    keys = [
        o["Key"]
        for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", [])
        if o["Key"].endswith(".log.gz")
    ]
    print(f"files: {len(keys)}  (prefix {prefix})")
    rows: list[dict] = []
    for k in keys:
        body = s3.get_object(Bucket=BUCKET, Key=k)["Body"].read()
        text = gzip.GzipFile(fileobj=io.BytesIO(body)).read().decode("utf-8", "replace")
        for line in text.splitlines():
            if not line.strip():
                continue
            parts = split_line(line)
            if len(parts) >= len(FIELDS):
                rows.append(dict(zip(FIELDS, parts, strict=False)))
    rows.sort(key=lambda r: r["time"])
    print(f"lines: {len(rows)}\n")
    return rows


def per_minute(rows: list[dict]) -> None:
    agg = defaultdict(lambda: {"total": 0, "fail": 0, "scanner": 0})
    for r in rows:
        m = r["time"][11:16]
        agg[m]["total"] += 1
        if not r["elb_status"].startswith("2"):
            agg[m]["fail"] += 1
        scanner_ua = ("Pentest", "Infrawatch", "zgrab")
        if any(s in r["user_agent"] for s in scanner_ua):
            agg[m]["scanner"] += 1
    print("=== per minute: requests / non-2xx / scanner ===")
    for m in sorted(agg):
        b = agg[m]
        print(f"  {m}  {b['total']:4d} {b['fail']:4d} {b['scanner']:4d}")


def non_2xx(rows: list[dict]) -> None:
    print("\n=== every non-2xx ===")
    for r in rows:
        if r["elb_status"].startswith("2"):
            continue
        print(
            f"  {r['time'][11:19]} elb={r['elb_status']:>3} target={r['target_status']:>3} "
            f"client={r['client'][:21]:21} err={r['error_reason'][:24]:24} "
            f"ua={r['user_agent'][:32]:32} {r['request'][:48]}"
        )


def user_agents(rows: list[dict]) -> None:
    print("\n=== user agents (scanners are obvious) ===")
    counts = defaultdict(int)
    for r in rows:
        counts[r["user_agent"][:60]] += 1
    for ua, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  {ua}")


def focus(rows: list[dict]) -> None:
    if not FOCUS_CLIENT:
        return
    hits = [r for r in rows if r["client"].startswith(FOCUS_CLIENT)]
    print(f"\n=== paths requested by {FOCUS_CLIENT} ({len(hits)} requests) ===")
    paths = defaultdict(int)
    for r in hits:
        paths[r["request"].split(" ")[1] if " " in r["request"] else r["request"]] += 1
    for p, n in sorted(paths.items(), key=lambda kv: -kv[1])[:25]:
        print(f"  {n:5d}  {p}")


if __name__ == "__main__":
    all_rows = load_rows()
    per_minute(all_rows)
    non_2xx(all_rows)
    user_agents(all_rows)
    focus(all_rows)
