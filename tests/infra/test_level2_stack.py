"""Level 2 stack assertions - the delivery and IAM-separation invariants.

These encode the promises the Level 2 design makes, so that a later change which
quietly breaks one (mutable tags, a public task, a task role that can read its own
secret, an OIDC trust policy that accepts any repository) fails the build rather
than being found in production.
"""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from infra.stacks.level2_ecs_stack import Level2EcsStack, Level2RegistryStack

GITHUB_REPO = "zor-neo/ai-incident-triage-poc"
GITHUB_BRANCH = "main"
IMAGE_TAG = "abc1234"
SECRET_PATH = "/incident-triage/level2/staging/internal-api-key"


@pytest.fixture(scope="module")
def registry_template() -> assertions.Template:
    app = cdk.App()
    stack = Level2RegistryStack(
        app,
        "TestRegistry",
        environment_name="level2",
        repository_name="incident-triage",
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    return assertions.Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> assertions.Template:
    app = cdk.App()
    stack = Level2EcsStack(
        app,
        "TestStack",
        environment_name="level2",
        model_id="amazon.nova-micro-v1:0",
        git_sha="testsha",
        image_tag=IMAGE_TAG,
        repository_name="incident-triage",
        github_repo=GITHUB_REPO,
        github_branch=GITHUB_BRANCH,
        alert_email="ops@example.com",
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    return assertions.Template.from_stack(stack)


def _statements(template: assertions.Template, resource_type: str) -> list[dict]:
    policies = template.find_resources(resource_type)
    return [
        s
        for p in policies.values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
    ]


def _all_actions(template: assertions.Template) -> list[str]:
    actions: list[str] = []
    policies = template.find_resources("AWS::IAM::Policy")
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement.get("Action", [])
            actions.extend(action if isinstance(action, list) else [action])
    return actions


# --- Registry ---------------------------------------------------------------


def test_images_are_immutable_and_scanned_on_push(registry_template: assertions.Template):
    """A tag must never be repointed, and every push must be scanned."""
    registry_template.has_resource_properties(
        "AWS::ECR::Repository", {"ImageTagMutability": "IMMUTABLE"}
    )
    registry_template.has_resource_properties(
        "AWS::ECR::Repository",
        {"ImageScanningConfiguration": {"ScanOnPush": True}},
    )
    # Untagged layers and old releases are cleaned up so the registry does not
    # silently accumulate storage cost.
    registry_template.has_resource_properties("AWS::ECR::Repository", {"LifecyclePolicy": {}})


# --- Compute ----------------------------------------------------------------


def test_two_fargate_services_one_per_environment(template: assertions.Template):
    template.resource_count_is("AWS::ECS::Service", 2)
    template.resource_count_is("AWS::ECS::TaskDefinition", 2)

    services = template.find_resources("AWS::ECS::Service")
    names = sorted(s["Properties"]["ServiceName"] for s in services.values())
    assert names == ["incident-triage-prod", "incident-triage-staging"]

    task_defs = template.find_resources("AWS::ECS::TaskDefinition")
    for task_def in task_defs.values():
        props = task_def["Properties"]
        assert props["RequiresCompatibilities"] == ["FARGATE"]
        (container,) = props["ContainerDefinitions"]
        # 0.25 vCPU / 512 MiB: the smallest sane Fargate size for this service.
        assert props["Cpu"] == "256"
        assert props["Memory"] == "512"
        assert container["Essential"] is True


def test_tasks_are_private_and_only_reachable_from_the_alb(template: assertions.Template):
    """No public IP; the ALB reaches :8000 by security-group reference."""
    for service in template.find_resources("AWS::ECS::Service").values():
        network = service["Properties"]["NetworkConfiguration"]
        assert network["AwsvpcConfiguration"]["AssignPublicIp"] == "DISABLED"

    groups = template.find_resources("AWS::EC2::SecurityGroup")
    task_group_id = next(
        k
        for k, v in groups.items()
        if "ALB security group only" in v["Properties"]["GroupDescription"]
    )
    alb_group_id = next(
        k
        for k, v in groups.items()
        if "Public HTTP ingress" in v["Properties"]["GroupDescription"]
    )
    task_group = groups[task_group_id]

    # Rules that reference another security group are emitted as separate
    # resources (by-reference rules cannot live inline without a circular
    # dependency), so the ingress is asserted there.
    assert "SecurityGroupIngress" not in task_group["Properties"]
    ingress_rules = [
        r["Properties"]
        for r in template.find_resources("AWS::EC2::SecurityGroupIngress").values()
        if task_group_id in json.dumps(r["Properties"].get("GroupId"))
    ]
    assert len(ingress_rules) == 1, "the task group must have exactly one inbound rule"
    (rule,) = ingress_rules
    assert rule["FromPort"] == 8000
    assert rule["ToPort"] == 8000
    assert alb_group_id in json.dumps(
        rule.get("SourceSecurityGroupId")
    ), "ingress must reference the ALB security group"
    assert "CidrIp" not in rule
    assert "CidrIpv6" not in rule


def test_both_environments_are_behind_the_one_alb(template: assertions.Template):
    listeners = template.find_resources("AWS::ElasticLoadBalancingV2::Listener")
    assert sorted(listener["Properties"]["Port"] for listener in listeners.values()) == [80, 8080]
    template.resource_count_is("AWS::ElasticLoadBalancingV2::TargetGroup", 2)
    for target_group in template.find_resources(
        "AWS::ElasticLoadBalancingV2::TargetGroup"
    ).values():
        props = target_group["Properties"]
        assert props["HealthCheckPath"] == "/health"
        # Same tuning as Level 1 INC-0002: the ALB is the debounce, so the alarm
        # does not add a second one.
        assert props["HealthCheckIntervalSeconds"] == 15
        assert props["UnhealthyThresholdCount"] == 3
        assert props["HealthyThresholdCount"] == 2
        assert props["TargetType"] == "ip"


# --- Secret injection -------------------------------------------------------


def test_the_secret_is_injected_from_ssm_and_never_in_the_template(template: assertions.Template):
    """The container gets INTERNAL_API_KEY from SSM, not from the template."""
    for task_def in template.find_resources("AWS::ECS::TaskDefinition").values():
        (container,) = task_def["Properties"]["ContainerDefinitions"]
        secrets = {s["Name"]: s for s in container["Secrets"]}
        assert "INTERNAL_API_KEY" in secrets
        # The imported parameter's ARN is assembled at deploy time, so assert on
        # the rendered form rather than a plain string.
        rendered = str(secrets["INTERNAL_API_KEY"]["ValueFrom"])
        assert "parameter/incident-triage" in rendered
        assert "internal-api-key" in rendered
        # It must not also be a plaintext environment variable.
        env_names = {e["Name"] for e in container.get("Environment", [])}
        assert "INTERNAL_API_KEY" not in env_names

    # No parameter resource (and therefore no value) in this template: the
    # SecureString is created out of band and only referenced by name.
    template.resource_count_is("AWS::SSM::Parameter", 0)


# --- IAM separation ---------------------------------------------------------


def test_execution_and_task_roles_are_distinct_and_disjoint(template: assertions.Template):
    """The whole point of this level: the roles are not interchangeable."""
    roles = template.find_resources("AWS::IAM::Role")
    by_description = {v["Properties"].get("Description", ""): k for k, v in roles.items()}

    exec_role_id = next(
        k for k, v in roles.items() if "ECS agent role" in v["Properties"].get("Description", "")
    )
    task_role_ids = [
        k for k, v in roles.items() if "Application role" in v["Properties"].get("Description", "")
    ]
    assert len(task_role_ids) == 2
    assert exec_role_id not in task_role_ids
    assert by_description  # roles carry descriptions that state their purpose

    def actions_for(role_id: str) -> list[str]:
        actions: list[str] = []
        for policy in template.find_resources("AWS::IAM::Policy").values():
            if role_id in str(policy["Properties"].get("Roles", [])):
                for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
                    action = statement.get("Action", [])
                    actions.extend(action if isinstance(action, list) else [action])
        return actions

    task_actions = [a for role in task_role_ids for a in actions_for(role)]

    # The application may read exactly its own data surface...
    assert any(a.startswith("dynamodb:") for a in task_actions)
    assert "bedrock:InvokeModel" in task_actions
    # ...and may NOT do any of the ECS-agent jobs.
    assert not any(a.startswith("ecr:") for a in task_actions)
    assert not any(a.startswith("logs:") for a in task_actions)
    assert not any(a.startswith("ssm:") for a in task_actions)
    assert not any(a.startswith("kms:") for a in task_actions)


def test_task_role_cannot_delete_or_scan_the_table(template: assertions.Template):
    actions = _all_actions(template)
    assert "dynamodb:PutItem" in actions
    assert "dynamodb:UpdateItem" in actions
    assert "dynamodb:GetItem" in actions
    assert "dynamodb:DeleteItem" not in actions
    assert "dynamodb:Scan" not in actions
    assert "dynamodb:Query" not in actions


def test_model_invocation_is_pinned_to_one_model(template: assertions.Template):
    resources: list[str] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            if statement.get("Action") == "bedrock:InvokeModel":
                resource = statement["Resource"]
                resources.extend(resource if isinstance(resource, list) else [resource])
    assert resources, "bedrock:InvokeModel must be scoped"
    assert not any(r == "*" for r in resources)
    assert any("foundation-model/amazon.nova-micro-v1:0" in str(r) for r in resources)


# --- Deploy role / GitHub OIDC ---------------------------------------------


def test_oidc_provider_is_imported_not_created(template: assertions.Template):
    """The account already has a GitHub provider; creating a second one fails."""
    template.resource_count_is("AWS::IAM::OIDCProvider", 0)


def test_deploy_trust_is_scoped_to_this_repo_branch_and_environment(template: assertions.Template):
    roles = template.find_resources("AWS::IAM::Role")
    deploy = next(
        v
        for v in roles.values()
        if "GitHub Actions via OIDC" in v["Properties"].get("Description", "")
    )
    (statement,) = deploy["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"

    conditions = statement["Condition"]
    aud = conditions["StringEquals"][
        "token.actions.githubusercontent.com:aud"
    ]
    assert aud == "sts.amazonaws.com"

    subjects = conditions["StringLike"]["token.actions.githubusercontent.com:sub"]
    subjects = subjects if isinstance(subjects, list) else [subjects]
    assert f"repo:{GITHUB_REPO}:ref:refs/heads/{GITHUB_BRANCH}" in subjects
    assert f"repo:{GITHUB_REPO}:environment:production" in subjects
    # No unqualified wildcard that would let any repository in.
    assert not any(s.endswith(":*") or s == "*" for s in subjects)
    assert not any("repo:*" in s for s in subjects)


def test_deploy_role_can_only_pass_the_two_task_role_pairs(template: assertions.Template):
    roles = template.find_resources("AWS::IAM::Role")
    deploy_id = next(
        k
        for k, v in roles.items()
        if "GitHub Actions via OIDC" in v["Properties"].get("Description", "")
    )
    pass_role_statements = [
        s
        for policy in template.find_resources("AWS::IAM::Policy").values()
        if deploy_id in str(policy["Properties"].get("Roles", []))
        for s in policy["Properties"]["PolicyDocument"]["Statement"]
        if s.get("Action") == "iam:PassRole"
    ]
    assert len(pass_role_statements) == 1
    (statement,) = pass_role_statements
    resources = statement["Resource"]
    resources = resources if isinstance(resources, list) else [resources]
    assert len(resources) == 4, "two task roles + two execution roles"
    assert all(r != "*" for r in resources)
    assert (
        statement["Condition"]["StringEquals"]["iam:PassedToService"]
        == "ecs-tasks.amazonaws.com"
    )


def test_deploy_role_cannot_touch_application_data(template: assertions.Template):
    roles = template.find_resources("AWS::IAM::Role")
    deploy_id = next(
        k
        for k, v in roles.items()
        if "GitHub Actions via OIDC" in v["Properties"].get("Description", "")
    )
    deploy_actions: list[str] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        if deploy_id in str(policy["Properties"].get("Roles", [])):
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
                action = statement.get("Action", [])
                deploy_actions.extend(action if isinstance(action, list) else [action])

    assert "ecs:UpdateService" in deploy_actions
    assert "ecr:PutImage" in deploy_actions
    assert not any(a.startswith("dynamodb:") for a in deploy_actions)
    assert "bedrock:InvokeModel" not in deploy_actions


# --- Data -------------------------------------------------------------------


def test_one_private_table_per_environment(template: assertions.Template):
    tables = template.find_resources("AWS::DynamoDB::Table")
    names = sorted(t["Properties"]["TableName"] for t in tables.values())
    assert names == ["IncidentTriage-prod", "IncidentTriage-staging"]
    for table in tables.values():
        props = table["Properties"]
        assert props["BillingMode"] == "PAY_PER_REQUEST"
        assert props["SSESpecification"]["SSEEnabled"] is True


def test_no_static_credentials_anywhere(template: assertions.Template):
    """No IAM users, no access keys - OIDC and roles only."""
    template.resource_count_is("AWS::IAM::User", 0)
    template.resource_count_is("AWS::IAM::AccessKey", 0)
    body = str(template.to_json())
    assert "AKIA" not in body
