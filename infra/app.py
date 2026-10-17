#!/usr/bin/env python3
"""CDK entrypoint.

Every level's stacks are defined here so that a single credential-free
``cdk synth`` (and therefore a single CI gate) validates all of them. Deploys
select individual stacks by name, e.g.::

    cdk deploy IncidentTriage-level1                      # Level 1 (EC2)
    cdk deploy IncidentTriage-level2-registry             # Level 2 registry first
    cdk deploy IncidentTriage-level2 -c imageTag=$SHA     # then the services

Context values (all optional, set in cdk.json or via ``-c key=value``):
  environment        Level 1 deployment tier, used in stack/resource names (default: level1)
  level2Environment  Level 2 deployment tier (default: level2)
  bedrockModelId     Bedrock model for triage (default: amazon.nova-micro-v1:0)
  alertEmail         email subscribed to the alarm/budget SNS topic (default: none)
  gitSha             version string surfaced by GET /health (default: local git HEAD)
  repositoryName     ECR repository name for the Level 2 image (default: incident-triage)
  imageTag           image tag the Level 2 task definitions start on (default: gitSha)
  githubRepoSubject  OIDC subject allowed to assume the deploy role, in GitHub's
                     immutable form `owner@ownerId/repo@repoId` (default: below)
  githubBranch       deploy branch allowed to assume the deploy role (default: main)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks
from stacks.level1_ec2_stack import Level1Ec2Stack
from stacks.level2_ecs_stack import Level2EcsStack, Level2RegistryStack

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
level2_environment: str = app.node.try_get_context("level2Environment") or "level2"
model_id: str = app.node.try_get_context("bedrockModelId") or "amazon.nova-micro-v1:0"
alert_email: str | None = app.node.try_get_context("alertEmail")
git_sha: str = app.node.try_get_context("gitSha") or _git_sha()

repository_name: str = app.node.try_get_context("repositoryName") or "incident-triage"
image_tag: str = app.node.try_get_context("imageTag") or git_sha
# The OIDC subject is `owner@ownerId/repo@repoId` - GitHub's immutable form.
# Find yours with:
#   aws cloudtrail lookup-events --lookup-attributes \
#     AttributeKey=EventName,AttributeValue=AssumeRoleWithWebIdentity
# and read userIdentity.userName, or from the OIDC token's `sub` claim.
github_repo_subject: str = (
    app.node.try_get_context("githubRepoSubject")
    or "zor-neo@240544755/ai-incident-triage-poc@1423586245"
)
github_branch: str = app.node.try_get_context("githubBranch") or "main"

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

Level2RegistryStack(
    app,
    f"IncidentTriage-{level2_environment}-registry",
    environment_name=level2_environment,
    repository_name=repository_name,
    env=env,
    description="AI Incident & Log Triage Service - Level 2 container registry",
)

Level2EcsStack(
    app,
    f"IncidentTriage-{level2_environment}",
    environment_name=level2_environment,
    model_id=model_id,
    git_sha=git_sha,
    image_tag=image_tag,
    repository_name=repository_name,
    github_repo_subject=github_repo_subject,
    github_branch=github_branch,
    alert_email=alert_email,
    env=env,
    description="AI Incident & Log Triage Service - Level 2 ECS Fargate delivery",
)

cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
