#!/usr/bin/env python3
"""ECR image-scan gate: block promotion on HIGH or CRITICAL findings.

Policy (technical spec section 6.2, item 7): an image carrying a HIGH or CRITICAL
vulnerability must never be promoted. The gate fails *closed* - a FAILED or
UNSUPPORTED scan, or one that never starts, is a failure, not a pass - because
"we could not tell" is not a reason to ship.

There is exactly one way past it: an entry in security/scan-exceptions.json.
Each entry names a CVE, states why the vulnerable code is unreachable in this
service, and carries an owner and a review date. An expired entry is treated as a
failure, so a waiver cannot be quietly inherited by a future release.

    uv run --no-sync python scripts/ecr_scan_gate.py incident-triage c0beb26
    uv run --no-sync python scripts/ecr_scan_gate.py incident-triage c0beb26 --timeout 900
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

BLOCKING = ("CRITICAL", "HIGH")
TERMINAL_OK = {"COMPLETE"}
TERMINAL_BAD = {"FAILED", "UNSUPPORTED"}

DEFAULT_EXCEPTIONS = Path("security/scan-exceptions.json")

# ECR cannot scan these. An OCI image *index* is what BuildKit produces when it
# attaches provenance/SBOM attestations, which is why the build must pass
# --provenance=false --sbom=false (INC-0003).
UNSCANNABLE_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
ACCEPTED_MEDIA_TYPES = [
    *sorted(UNSCANNABLE_MEDIA_TYPES),
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
]


def manifest_media_type(ecr, repository: str, tag: str) -> str | None:
    """The stored manifest's media type, or None if it cannot be read."""
    try:
        response = ecr.batch_get_image(
            repositoryName=repository,
            imageIds=[{"imageTag": tag}],
            acceptedMediaTypes=ACCEPTED_MEDIA_TYPES,
        )
    except ClientError:
        return None
    images = response.get("images", [])
    return images[0].get("imageManifestMediaType") if images else None


def scan_findings(ecr, repository: str, tag: str) -> tuple[str, dict, list[dict]]:
    """Return (scan status, severity counts, every finding) for one image tag.

    Paginates: ECR returns findings in pages, and a gate that only reads the
    first page has a blind spot exactly where a large image has the most to hide.
    """
    try:
        response = ecr.describe_image_scan_findings(
            repositoryName=repository,
            imageId={"imageTag": tag},
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in {"ImageNotFoundException", "ScanNotFoundException"}:
            # Nothing to read yet: either the push is not indexed or no scan ran.
            return "PENDING", {}, []
        raise

    status = response.get("imageScanStatus", {}).get("status", "PENDING")
    findings_doc = response.get("imageScanFindings", {}) or {}
    counts = findings_doc.get("findingSeverityCounts", {}) or {}
    findings = list(findings_doc.get("findings", []) or [])

    next_token = findings_doc.get("nextToken")
    while next_token and status in TERMINAL_OK:
        page = ecr.describe_image_scan_findings(
            repositoryName=repository,
            imageId={"imageTag": tag},
            nextToken=next_token,
        )
        page_doc = page.get("imageScanFindings", {}) or {}
        findings.extend(page_doc.get("findings", []) or [])
        next_token = page_doc.get("nextToken")

    return status, counts, findings


def load_exceptions(path: Path) -> tuple[dict[str, dict], list[dict]]:
    """Return (active exceptions keyed by CVE, expired exceptions)."""
    if not path.exists():
        return {}, []
    document = json.loads(path.read_text(encoding="utf-8"))
    today = date.today()
    active: dict[str, dict] = {}
    expired: list[dict] = []
    for entry in document.get("exceptions", []):
        review_by = date.fromisoformat(entry["review_by"])
        if review_by < today:
            expired.append(entry)
        else:
            active[entry["cve"]] = entry
    return active, expired


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
    parser.add_argument(
        "--exceptions",
        type=Path,
        default=DEFAULT_EXCEPTIONS,
        help="JSON file of time-boxed exceptions (default: %(default)s)",
    )
    args = parser.parse_args()

    ecr = boto3.client("ecr", region_name=args.region)
    active_exceptions, expired_exceptions = load_exceptions(args.exceptions)

    deadline = time.monotonic() + args.timeout
    started = time.monotonic()
    status, counts, findings = "PENDING", {}, []
    while time.monotonic() < deadline:
        status, counts, findings = scan_findings(ecr, args.repository, args.tag)
        if status in TERMINAL_OK or status in TERMINAL_BAD:
            break
        # No scan is ever going to appear for an unscannable artifact. Detect it
        # by reading the stored manifest rather than waiting out the full timeout
        # and reporting a vague "did not complete" 15 minutes later.
        if time.monotonic() - started > 45:
            media_type = manifest_media_type(ecr, args.repository, args.tag)
            if media_type in UNSCANNABLE_MEDIA_TYPES:
                print(f"FAIL: {args.repository}:{args.tag} was pushed as {media_type}")
                print(
                    "      ECR cannot scan an OCI image index (what BuildKit produces "
                    "when it attaches provenance/SBOM attestations)."
                )
                print(
                    "      Build with: docker build --platform linux/amd64 "
                    "--provenance=false --sbom=false"
                )
                return 1
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

    blocking = [
        finding
        for finding in findings
        if finding.get("severity") in BLOCKING and finding.get("name") not in active_exceptions
    ]
    allowed = [
        finding
        for finding in findings
        if finding.get("severity") in BLOCKING and finding.get("name") in active_exceptions
    ]

    for finding in allowed:
        entry = active_exceptions[finding["name"]]
        print(
            f"allowed : {finding['name']} [{finding.get('severity')}] "
            f"{entry.get('package', '?')} - owner {entry.get('owner')}, "
            f"review by {entry.get('review_by')}"
        )

    if expired_exceptions:
        print("FAIL: the exception file contains expired entries; renew or remove them:")
        for entry in expired_exceptions:
            print(f"  {entry['cve']} (review_by {entry['review_by']})")
        return 1

    if blocking:
        print(f"FAIL: {len(blocking)} blocking vulnerability(ies)")
        for finding in blocking:
            print(describe_finding(finding))
        print(
            "      To promote anyway, add a time-boxed entry to "
            f"{args.exceptions} that explains why the code is unreachable."
        )
        return 1

    print("PASS: no unexcepted HIGH or CRITICAL findings; the image is promotable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
