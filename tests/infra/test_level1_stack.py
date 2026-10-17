"""Level 1 stack assertions - the security invariants that matter at this level."""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from infra.stacks.level1_ec2_stack import Level1Ec2Stack

TEST_EMAIL = "ops@example.com"


@pytest.fixture(scope="module")
def template() -> assertions.Template:
    app = cdk.App()
    stack = Level1Ec2Stack(
        app,
        "TestStack",
        environment_name="level1",
        model_id="amazon.nova-micro-v1:0",
        alert_email=TEST_EMAIL,
        git_sha="testsha",
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    return assertions.Template.from_stack(stack)


def _actions(statement: dict) -> list[str]:
    action = statement.get("Action", [])
    return action if isinstance(action, list) else [action]


def _statements(template: assertions.Template) -> list[dict]:
    policies = template.find_resources("AWS::IAM::Policy")
    return [s for p in policies.values() for s in p["Properties"]["PolicyDocument"]["Statement"]]


def test_instance_is_private_and_arm(template: assertions.Template):
    subnets = template.find_resources("AWS::EC2::Subnet")
    public = {k for k, v in subnets.items() if v["Properties"].get("MapPublicIpOnLaunch")}
    private = set(subnets) - public
    assert len(public) == 2
    assert len(private) == 2

    instances = template.find_resources("AWS::EC2::Instance")
    assert len(instances) == 1
    props = next(iter(instances.values()))["Properties"]
    assert props["InstanceType"] == "t4g.small"
    assert props["SubnetId"] in [{"Ref": subnet} for subnet in private]
    (block_device,) = props["BlockDeviceMappings"]
    assert block_device["Ebs"]["Encrypted"] is True
    assert block_device["Ebs"]["VolumeSize"] == 8

    # require_imdsv2=True is expressed through a launch template.
    template.has_resource_properties(
        "AWS::EC2::LaunchTemplate",
        {"LaunchTemplateData": {"MetadataOptions": {"HttpTokens": "required"}}},
    )


def test_app_security_group_only_references_alb(template: assertions.Template):
    groups = template.find_resources("AWS::EC2::SecurityGroup")
    app_group_ids = {
        k
        for k, v in groups.items()
        if "ALB security group" in v["Properties"]["GroupDescription"]
    }
    alb_group_ids = {
        k
        for k, v in groups.items()
        if "load balancer" in v["Properties"]["GroupDescription"]
    }
    assert len(app_group_ids) == 1
    assert len(alb_group_ids) == 1

    ingresses = template.find_resources("AWS::EC2::SecurityGroupIngress")
    app_ingress = [
        v["Properties"]
        for v in ingresses.values()
        if v["Properties"]["GroupId"]["Fn::GetAtt"][0] in app_group_ids
    ]
    assert app_ingress, "expected an explicit ingress rule on the app security group"
    for rule in app_ingress:
        assert "CidrIp" not in rule, "app ingress must not use broad CIDR rules"
        assert rule["SourceSecurityGroupId"]["Fn::GetAtt"][0] in alb_group_ids
        assert rule["FromPort"] == 8000

    # Only the ALB security group may be reachable from the internet.
    publicly_open = {
        logical
        for logical, v in groups.items()
        for rule in v["Properties"].get("SecurityGroupIngress") or []
        if rule.get("CidrIp") == "0.0.0.0/0"
    }
    assert publicly_open == alb_group_ids


def test_no_static_credentials(template: assertions.Template):
    template.resource_count_is("AWS::IAM::User", 0)
    template.resource_count_is("AWS::IAM::AccessKey", 0)


def test_instance_role_permissions_are_scoped(template: assertions.Template):
    statements = _statements(template)

    bedrock = [s for s in statements if "bedrock:InvokeModel" in _actions(s)]
    assert bedrock, "expected a bedrock:InvokeModel statement"
    bedrock_resources = json.dumps([s["Resource"] for s in bedrock])
    assert "foundation-model/amazon.nova-micro-v1:0" in bedrock_resources
    assert "inference-profile/*" in bedrock_resources

    ddb = [s for s in statements if any("dynamodb:" in a for a in _actions(s))]
    assert ddb, "expected a DynamoDB statement"
    for statement in ddb:
        assert set(_actions(statement)) == {
            "dynamodb:PutItem",
            "dynamodb:GetItem",
            "dynamodb:UpdateItem",
        }
        assert "*" not in json.dumps(statement["Resource"])

    metrics = [s for s in statements if "cloudwatch:PutMetricData" in _actions(s)]
    assert metrics
    assert metrics[0]["Condition"]["StringEquals"]["cloudwatch:namespace"] == "IncidentTriage"


def test_gateway_endpoints_keep_aws_traffic_off_nat(template: assertions.Template):
    endpoints = template.find_resources("AWS::EC2::VPCEndpoint")
    assert len(endpoints) == 2
    services = [json.dumps(v["Properties"]["ServiceName"]) for v in endpoints.values()]
    assert all(v["Properties"]["VpcEndpointType"] == "Gateway" for v in endpoints.values())
    assert sum("dynamodb" in s for s in services) == 1
    assert sum(".s3" in s for s in services) == 1


def test_data_store_is_on_demand_and_encrypted(template: assertions.Template):
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "TableName": "IncidentTriage-level1",
            "BillingMode": "PAY_PER_REQUEST",
            "SSESpecification": {"SSEEnabled": True},
        },
    )


def test_buckets_block_public_access(template: assertions.Template):
    buckets = template.find_resources("AWS::S3::Bucket")
    assert len(buckets) == 2
    for bucket in buckets.values():
        assert bucket["Properties"]["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }
        assert "BucketEncryption" in bucket["Properties"]


def test_audit_and_access_logging_are_enabled(template: assertions.Template):
    template.has_resource_properties(
        "AWS::CloudTrail::Trail",
        {"EnableLogFileValidation": True, "IsMultiRegionTrail": False},
    )
    template.has_resource_properties(
        "AWS::Logs::LogGroup",
        {"LogGroupName": "/aws/ec2/incident-triage", "RetentionInDays": 7},
    )
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        {"Scheme": "internet-facing"},
    )
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        {"LoadBalancerAttributes": assertions.Match.array_with([
            {"Key": "access_logs.s3.enabled", "Value": "true"},
        ])},
    )


def test_alarms_and_budget_are_wired_to_the_alert_topic(template: assertions.Template):
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    assert len(alarms) == 3
    names = {v["Properties"]["AlarmName"] for v in alarms.values()}
    assert names == {
        "TestStack-alb-unhealthy-targets",
        "TestStack-alb-5xx",
        "TestStack-ec2-cpu",
    }
    for alarm in alarms.values():
        actions = alarm["Properties"].get("AlarmActions", [])
        assert actions, "every alarm must notify the alert topic"

    template.has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": TEST_EMAIL}
    )
    template.has_resource_properties(
        "AWS::Budgets::Budget",
        {
            "Budget": {
                "BudgetType": "COST",
                "TimeUnit": "MONTHLY",
                "BudgetLimit": {"Amount": 10, "Unit": "USD"},
            }
        },
    )


def test_all_resources_are_tagged(template: assertions.Template):
    instances = template.find_resources("AWS::EC2::Instance")
    tags = {t["Key"]: t["Value"] for t in next(iter(instances.values()))["Properties"]["Tags"]}
    assert tags["Project"] == "IncidentTriage"
    assert tags["Environment"] == "level1"
    assert tags["ManagedBy"] == "AWSCDK"
