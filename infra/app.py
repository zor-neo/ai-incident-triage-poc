#!/usr/bin/env python3
"""CDK entrypoint.

Context values (all optional, set in cdk.json or via ``-c key=value``):
  environment     deployment tier name, used in stack/resource names (default: level1)
  bedrockModelId  Bedrock model for triage (default: amazon.nova-micro-v1:0)
  alertEmail      email subscribed to the alarm/budget SNS topic (default: none)
  gitSha          version string surfaced by GET /health (default: local git HEAD)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks
from stacks.level1_ec2_stack import Level1Ec2Stack

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip() or "uncommitted"
    except Exception:  # noqa: BLE001 - best-effort version stamp
        return "uncommitted"


app = cdk.App()

environment_name: str = app.node.try_get_context("environment") or "level1"
model_id: str = app.node.try_get_context("bedrockModelId") or "amazon.nova-micro-v1:0"
alert_email: str | None = app.node.try_get_context("alertEmail")
git_sha: str = app.node.try_get_context("gitSha") or _git_sha()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION") or "us-east-1",
)

Level1Ec2Stack(
    app,
    f"IncidentTriage-{environment_name}",
    environment_name=environment_name,
    model_id=model_id,
    alert_email=alert_email,
    git_sha=git_sha,
    env=env,
    description="AI Incident & Log Triage Service - Level 1 CloudOps Foundation",
)

cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
