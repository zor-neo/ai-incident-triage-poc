#!/usr/bin/env python
"""Promote an immutable image to an ECS service - the Level 2 deploy command.

The pipeline does not run ``cdk deploy``. CloudFormation owns the *structure* of
the task definition (cpu, roles, secrets, log config); this script owns only the
*artifact* - it clones the current revision, repoints the container at a specific
image tag, records that tag in GIT_SHA, registers the new revision and moves the
service onto it. That split is what keeps the deploy role narrow: it needs ECS and
ECR, not CloudFormation and not the ability to change IAM.

It is also the rollback command, because rolling back is the same operation aimed
at an older immutable tag:

    uv run --no-sync python scripts/deploy_ecs_image.py \
        --service incident-triage-staging --image-tag 5d3733a

    uv run --no-sync python scripts/deploy_ecs_image.py \
        --service incident-triage-prod --image-tag 5d3733a
"""

from __future__ import annotations

import argparse
import time

import boto3
from botocore.exceptions import ClientError

DEFAULT_CLUSTER = "incident-triage-level2"
CONTAINER_NAME = "app"


def latest_task_definition(ecs, family: str) -> dict:
    described = ecs.describe_task_definition(taskDefinition=family)
    return described["taskDefinition"]


def build_registration(previous: dict, image_uri: str, git_sha: str) -> dict:
    """Turn a described task definition back into a registerable payload."""
    read_only = {
        "taskDefinitionArn",
        "revision",
        "status",
        "requiresAttributes",
        "compatibilities",
        "registeredAt",
        "registeredBy",
        "deregisteredAt",
    }
    payload = {k: v for k, v in previous.items() if k not in read_only}

    containers = []
    for container in payload["containerDefinitions"]:
        container = dict(container)
        if container.get("name") == CONTAINER_NAME:
            container["image"] = image_uri
            environment = [e for e in container.get("environment", []) if e["name"] != "GIT_SHA"]
            environment.append({"name": "GIT_SHA", "value": git_sha})
            container["environment"] = environment
        containers.append(container)
    payload["containerDefinitions"] = containers
    return payload


def wait_for_stability(ecs, cluster: str, service: str, timeout: int) -> str:
    """Poll until the PRIMARY deployment settles. Returns COMPLETED or FAILED."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        described = ecs.describe_services(cluster=cluster, services=[service])
        (current,) = described["services"]
        primary = next(
            (d for d in current.get("deployments", []) if d["status"] == "PRIMARY"), None
        )
        if primary is None:
            time.sleep(10)
            continue
        state = primary["rolloutState"]
        print(
            f"  rollout={state} running={primary['runningCount']}/"
            f"{primary['desiredCount']} pending={primary['pendingCount']} "
            f"failed={primary['failedTasks']}"
        )
        if current["runningCount"] == current["desiredCount"] and state == "COMPLETED":
            return "COMPLETED"
        if state == "FAILED":
            return "FAILED"
        time.sleep(15)
    return "TIMEOUT"


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy an image tag to an ECS service")
    parser.add_argument(
        "--service", required=True, help="ECS service name, e.g. incident-triage-staging"
    )
    parser.add_argument("--image-tag", required=True, help="immutable tag = commit SHA")
    parser.add_argument("--repository", default="incident-triage", help="ECR repository name")
    parser.add_argument("--cluster", default=DEFAULT_CLUSTER, help="ECS cluster name")
    parser.add_argument("--region", default=None, help="AWS region")
    parser.add_argument("--timeout", type=int, default=600, help="seconds to wait for stability")
    args = parser.parse_args()

    ecs = boto3.client("ecs", region_name=args.region)
    ecr = boto3.client("ecr", region_name=args.region)

    try:
        repository = ecr.describe_repositories(repositoryNames=[args.repository])["repositories"][0]
    except ClientError as exc:
        print(f"FAIL: cannot describe ECR repository {args.repository}: {exc}")
        return 1
    image_uri = f"{repository['repositoryUri']}:{args.image_tag}"

    # Fail in two seconds if the tag was never pushed. Without this guard the
    # mistake surfaces four minutes later as CannotPullContainerError, a circuit
    # breaker trip, a CloudFormation rollback and a stack that then has to be
    # deleted before it can be deployed again.
    try:
        ecr.describe_images(
            repositoryName=args.repository,
            imageIds=[{"imageTag": args.image_tag}],
        )
    except ClientError:
        print(f"FAIL: {args.repository}:{args.image_tag} does not exist in ECR.")
        print("      Build and push it first, or pass the tag that was actually pushed:")
        recent = ecr.describe_images(
            repositoryName=args.repository,
            filter={"tagStatus": "TAGGED"},
        )["imageDetails"]
        tags = sorted(t for i in recent for t in i.get("imageTags", []))
        print(f"      tags in the repository: {', '.join(tags) or '(none)'}")
        return 1

    family = args.service
    previous = latest_task_definition(ecs, family)
    print(f"deploying {args.service}")
    print(f"  from revision {previous['revision']} -> {image_uri}")

    payload = build_registration(previous, image_uri, args.image_tag)
    registered = ecs.register_task_definition(**payload)["taskDefinition"]
    print(f"  registered revision {registered['revision']}")
    if registered["revision"] == previous["revision"]:
        print("FAIL: registration did not create a new revision")
        return 1

    ecs.update_service(
        cluster=args.cluster,
        service=args.service,
        taskDefinition=registered["taskDefinitionArn"],
        forceNewDeployment=True,
    )
    print("  service updated; waiting for a steady state")

    outcome = wait_for_stability(ecs, args.cluster, args.service, args.timeout)
    if outcome == "COMPLETED":
        print(f"PASS: {args.service} is serving {args.image_tag}")
        return 0
    print(f"FAIL: {args.service} deployment ended as {outcome}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
