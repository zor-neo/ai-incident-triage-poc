#!/usr/bin/env python3
"""ECR image-scan gate: block promotion on HIGH or CRITICAL findings.

Policy (technical spec section 6.2, item 7): an image carrying a HIGH or CRITICAL
vulnerability must never be promoted. The gate fails *closed* - an UNSUPPORTED or
FAILED scan is a failure, not a pass - because "we could not tell" is not a reason
to ship.

Used as a pipeline step between "push the image" and "deploy the image", and
runnable by hand against any tag:

    uv run --no-sync python scripts/ecr_scan_gate.py incident-triage abc1234
    uv run --no-sync python scripts/ecr_scan_gate.py incident-triage abc1234 --timeout 900
"""

from __future__ import annotations

import argparse
import time

import boto3
from botocore.exceptions import ClientError

BLOCKING = ("CRITICAL", "HIGH")
TERMINAL_OK = {"COMPLETE"}
TERMINAL_BAD = {"FAILED", "UNSUPPORTED"}


def scan_state(ecr, repository: str, tag: str) -> tuple[str, dict, list[dict]]:
    """Return (scan status, severity counts, findings) for one image tag."""
    try:
        response = ecr.describe_image_scan_findings(
            repositoryName=repository,
            imageId={"imageTag": tag},
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in {"ImageNotFoundException", "ScanNotFoundException"}:
            # The push has not been registered for scanning yet.
            return "PENDING", {}, []
        raise

    status = response.get("imageScanStatus", {}).get("status", "PENDING")
    findings_doc = response.get("imageScanFindings", {})
    counts = findings_doc.get("findingSeverityCounts", {}) or {}
    findings = findings_doc.get("findings", []) or []
    return status, counts, findings


def describe_finding(finding: dict) -> str:
    name = finding.get("name", "unknown")
    severity = finding.get("severity", "?")
    uri = finding.get("uri", "")
    return f"  [{severity:8}] {name} {uri}".rstrip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail if an image has HIGH/CRITICAL CVEs")
    parser.add_argument("repository", help="ECR repository name, e.g. incident-triage")
    parser.add_argument("tag", help="image tag, which by policy is the commit SHA")
    parser.add_argument("--region", default=None, help="AWS region (default: environment)")
    parser.add_argument("--timeout", type=int, default=600, help="seconds to wait for the scan")
    parser.add_argument("--interval", type=int, default=15, help="poll interval in seconds")
    args = parser.parse_args()

    ecr = boto3.client("ecr", region_name=args.region)

    deadline = time.monotonic() + args.timeout
    status, counts, findings = "PENDING", {}, []
    while time.monotonic() < deadline:
        status, counts, findings = scan_state(ecr, args.repository, args.tag)
        if status in TERMINAL_OK or status in TERMINAL_BAD:
            break
        print(f"scan of {args.repository}:{args.tag} is {status}; waiting...")
        time.sleep(args.interval)

    print(f"scan status: {status}")
    if counts:
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(f"severity counts: {summary}")

    if status in TERMINAL_BAD:
        print(f"FAIL: image scan did not complete successfully ({status}); refusing to promote")
        return 1
    if status not in TERMINAL_OK:
        print(f"FAIL: image scan did not complete within {args.timeout}s (last status: {status})")
        return 1

    blocking = {sev: counts.get(sev, 0) for sev in BLOCKING if counts.get(sev, 0) > 0}
    if blocking:
        total = sum(blocking.values())
        print(f"FAIL: {total} blocking vulnerability(ies) ({', '.join(blocking)})")
        for finding in findings:
            if finding.get("severity") in BLOCKING:
                print(describe_finding(finding))
        return 1

    print("PASS: no HIGH or CRITICAL findings; the image is promotable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
