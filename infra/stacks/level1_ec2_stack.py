"""Level 1 - CloudOps Foundation.

A deliberately simple but production-like deployment: public ALB -> private EC2
instance running the FastAPI service -> DynamoDB, with an IAM instance role
(no static keys), CloudWatch logs/alarms, CloudTrail auditing, an AWS Budget,
and cost-allocation tags. Everything is created by CDK so the environment can be
recreated or destroyed deterministically.

Network design (see docs/network-design.md):
  VPC 10.20.0.0/16
    Public  10.20.0.0/24, 10.20.1.0/24  - ALB + the single NAT gateway
    Private 10.20.2.0/24, 10.20.3.0/24  - EC2 app instance
  S3 and DynamoDB gateway endpoints keep asset download and table traffic off
  the NAT gateway (no per-GB NAT charges, no internet hop for AWS APIs).
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
    aws_budgets as budgets,
    aws_cloudtrail as cloudtrail,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_elasticloadbalancingv2 as elbv2,
    aws_elasticloadbalancingv2_targets as elbv2_targets,
    aws_iam as iam,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_assets as s3_assets,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subscriptions,
)
from constructs import Construct
from cdk_nag import NagPackSuppression, NagSuppressions

REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_GROUP_NAME = "/aws/ec2/incident-triage"

USER_DATA_TEMPLATE = r"""#!/bin/bash
set -euxo pipefail

# --- 1. Runtime packages -------------------------------------------------
# Amazon Linux 2023 ships 3.9 as `python3`; 3.12 is a separate package. Fall
# back to 3.11 if the 3.12 package is unavailable for this AMI/architecture.
if dnf install -y python3.12 python3.12-pip; then
  PYTHON=python3.12
else
  dnf install -y python3.11 python3.11-pip
  PYTHON=python3.11
fi
dnf install -y unzip amazon-cloudwatch-agent

# --- 2. Non-root service account and directories -------------------------
id -u appuser >/dev/null 2>&1 || \
  useradd --system --home /opt/incident-triage --shell /sbin/nologin appuser
install -d -o appuser -g appuser -m 0755 /opt/incident-triage
install -d -o appuser -g appuser -m 0755 /var/log/incident-triage

# --- 3. Application bundle (CDK S3 asset) --------------------------------
aws s3 cp "__ASSET_URL__" /tmp/app.zip
unzip -o /tmp/app.zip -d /opt/incident-triage
rm -f /tmp/app.zip

# --- 4. Deterministic Python install -------------------------------------
"$PYTHON" -m venv /opt/incident-triage/.venv
/opt/incident-triage/.venv/bin/pip install --no-cache-dir --upgrade pip
/opt/incident-triage/.venv/bin/pip install --no-cache-dir \
  -r /opt/incident-triage/requirements.lock.txt
chown -R appuser:appuser /opt/incident-triage

# --- 5. Runtime configuration --------------------------------------------
cat > /etc/incident-triage.env <<'ENVFILE'
ENVIRONMENT=__ENVIRONMENT__
AWS_REGION=__REGION__
AWS_DEFAULT_REGION=__REGION__
TABLE_NAME=__TABLE_NAME__
BEDROCK_MODEL_ID=__MODEL_ID__
GIT_SHA=__GIT_SHA__
LOG_LEVEL=INFO
METRICS_NAMESPACE=IncidentTriage
ENVFILE
chmod 0640 /etc/incident-triage.env
chown root:appuser /etc/incident-triage.env

# --- 6. systemd unit ------------------------------------------------------
cat > /etc/systemd/system/incident-triage.service <<'UNIT'
[Unit]
Description=AI Incident and Log Triage API (Level 1 EC2)
After=network-online.target
Wants=network-online.target

[Service]
User=appuser
Group=appuser
WorkingDirectory=/opt/incident-triage
EnvironmentFile=/etc/incident-triage.env
ExecStart=/opt/incident-triage/.venv/bin/uvicorn app.main:app \
  --host 0.0.0.0 --port 8000 --no-access-log
Restart=always
RestartSec=3
StandardOutput=append:/var/log/incident-triage/app.log
StandardError=append:/var/log/incident-triage/app.log
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now incident-triage

# --- 7. CloudWatch agent: ship the JSON app log to CloudWatch Logs --------
cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<'CWCONF'
{
  "agent": { "metrics_collection_interval": 60 },
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/incident-triage/app.log",
            "log_group_name": "__LOG_GROUP__",
            "log_stream_name": "{instance_id}",
            "retention_in_days": 7
          }
        ]
      }
    }
  }
}
CWCONF

/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 -s \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json

echo "incident-triage bootstrap complete"
"""


class Level1Ec2Stack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment_name: str,
        model_id: str,
        git_sha: str,
        alert_email: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.environment_name = environment_name
        self.model_id = model_id
        self.git_sha = git_sha

        # --- Network ------------------------------------------------------
        vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr("10.20.0.0/16"),
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
            gateway_endpoints={
                "DynamoDB": ec2.GatewayVpcEndpointOptions(
                    service=ec2.GatewayVpcEndpointAwsService.DYNAMODB
                ),
                "S3": ec2.GatewayVpcEndpointOptions(
                    service=ec2.GatewayVpcEndpointAwsService.S3
                ),
            },
            enable_dns_hostnames=True,
            enable_dns_support=True,
        )

        # --- Observability: log group ------------------------------------
        app_log_group = logs.LogGroup(
            self,
            "AppLogGroup",
            log_group_name=LOG_GROUP_NAME,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Data ---------------------------------------------------------
        table = dynamodb.Table(
            self,
            "IncidentTable",
            table_name=f"IncidentTriage-{environment_name}",
            partition_key=dynamodb.Attribute(
                name="incident_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            deletion_protection=False,
        )

        # --- Alerts -------------------------------------------------------
        alerts_topic = sns.Topic(
            self,
            "AlertsTopic",
            topic_name=f"IncidentTriage-alerts-{environment_name}",
            display_name="AI Incident Triage - operational alerts",
            enforce_ssl=True,
        )
        if alert_email:
            alerts_topic.add_subscription(sns_subscriptions.EmailSubscription(alert_email))

        alerts_topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowBudgetsPublish",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("budgets.amazonaws.com")],
                actions=["SNS:Publish"],
                resources=[alerts_topic.topic_arn],
                conditions={"StringEquals": {"aws:SourceAccount": cdk.Aws.ACCOUNT_ID}},
            )
        )

        # --- Security groups (by-reference, no broad CIDRs on the app) ----
        alb_sg = ec2.SecurityGroup(
            self,
            "AlbSecurityGroup",
            vpc=vpc,
            description="Public HTTP ingress for the application load balancer",
            allow_all_outbound=False,
        )
        alb_sg.add_ingress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(80),
            "HTTP from the internet (lab environment: no TLS certificate/domain yet)",
        )
        alb_sg.add_egress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS to AWS APIs if required by the ALB"
        )

        app_sg = ec2.SecurityGroup(
            self,
            "AppSecurityGroup",
            vpc=vpc,
            description="FastAPI traffic from the ALB security group only, by reference",
            allow_all_outbound=False,
        )
        app_sg.add_ingress_rule(
            alb_sg, ec2.Port.tcp(8000), "FastAPI from the ALB security group (reference)"
        )
        alb_sg.add_egress_rule(app_sg, ec2.Port.tcp(8000), "Forward requests to app instances")
        app_sg.add_egress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS to AWS APIs (Bedrock, CloudWatch, SSM)"
        )
        app_sg.add_egress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "HTTP for OS package repositories"
        )
        app_sg.add_egress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.udp(53), "DNS via the VPC resolver"
        )

        # --- Load balancer -------------------------------------------------
        alb = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            vpc=vpc,
            internet_facing=True,
            security_group=alb_sg,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            deletion_protection=False,
        )

        alb_logs_bucket = s3.Bucket(
            self,
            "AlbAccessLogsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            auto_delete_objects=True,
            removal_policy=RemovalPolicy.DESTROY,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(30))],
        )
        alb.log_access_logs(alb_logs_bucket)

        # --- Application bundle and instance role -------------------------
        app_asset = s3_assets.Asset(
            self,
            "AppAsset",
            path=str(REPO_ROOT),
            exclude=[
                ".git",
                ".venv",
                ".pytest_cache",
                ".ruff_cache",
                ".github",
                "cdk.out",
                "**/cdk.out",
                "**/__pycache__",
                "*.pyc",
                "tests",
                "docs",
                "infra",
                "scripts",
                "evidence",
                ".env",
                "*.md",
                ".gitignore",
            ],
        )

        instance_role = iam.Role(
            self,
            "InstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            description="Least-privilege role for the incident triage EC2 instance",
        )
        app_asset.grant_read(instance_role)
        table.grant(instance_role, "dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem")

        instance_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeTriageModel",
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:{cdk.Aws.PARTITION}:bedrock:{cdk.Aws.REGION}::foundation-model/{model_id}",
                    f"arn:{cdk.Aws.PARTITION}:bedrock:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}:inference-profile/*",
                ],
            )
        )
        instance_role.add_to_policy(
            iam.PolicyStatement(
                sid="PublishCustomMetrics",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": "IncidentTriage"}},
            )
        )
        instance_role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteApplicationLogs",
                actions=[
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                resources=[f"{app_log_group.log_group_arn}:*", app_log_group.log_group_arn],
            )
        )
        instance_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchAgentDiscovery",
                actions=["logs:DescribeLogGroups"],
                resources=["*"],
            )
        )
        # Minimum set required by SSM Session Manager (no bastion, no SSH keys).
        instance_role.add_to_policy(
            iam.PolicyStatement(
                sid="SessionManagerAccess",
                actions=[
                    "ssm:UpdateInstanceInformation",
                    "ssmmessages:CreateControlChannel",
                    "ssmmessages:CreateDataChannel",
                    "ssmmessages:OpenControlChannel",
                    "ssmmessages:OpenDataChannel",
                    "ec2messages:AcknowledgeMessage",
                    "ec2messages:DeleteMessage",
                    "ec2messages:GetMessages",
                    "ec2messages:SendReply",
                ],
                resources=["*"],
            )
        )

        app_instance = ec2.Instance(
            self,
            "AppInstance",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            instance_type=ec2.InstanceType("t4g.small"),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(
                cpu_type=ec2.AmazonLinuxCpuType.ARM_64
            ),
            security_group=app_sg,
            role=instance_role,
            require_imdsv2=True,
            user_data_causes_replacement=True,
            detailed_monitoring=False,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(
                        8, volume_type=ec2.EbsDeviceVolumeType.GP3, encrypted=True
                    ),
                )
            ],
        )

        user_data = (
            USER_DATA_TEMPLATE.replace("__ASSET_URL__", app_asset.s3_object_url)
            .replace("__ENVIRONMENT__", environment_name)
            .replace("__REGION__", self.region)
            .replace("__TABLE_NAME__", table.table_name)
            .replace("__MODEL_ID__", model_id)
            .replace("__GIT_SHA__", git_sha)
            .replace("__LOG_GROUP__", LOG_GROUP_NAME)
        )
        app_instance.add_user_data(user_data)

        # --- Target group and listener -------------------------------------
        target_group = elbv2.ApplicationTargetGroup(
            self,
            "AppTargetGroup",
            vpc=vpc,
            port=8000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[elbv2_targets.InstanceTarget(app_instance, port=8000)],
            deregistration_delay=Duration.seconds(15),
            health_check=elbv2.HealthCheck(
                path="/health",
                port="8000",
                protocol=elbv2.Protocol.HTTP,
                interval=Duration.seconds(15),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
                healthy_http_codes="200",
            ),
        )

        alb.add_listener(
            "HttpListener",
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            default_target_groups=[target_group],
        )

        # --- Alarms ---------------------------------------------------------
        unhealthy_alarm = cloudwatch.Alarm(
            self,
            "UnhealthyTargetsAlarm",
            alarm_name=f"{construct_id}-alb-unhealthy-targets",
            metric=target_group.metrics.unhealthy_host_count(period=Duration.minutes(1)),
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=2,
            datapoints_to_alarm=2,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="One or more ALB targets failed the /health check",
        )
        unhealthy_alarm.add_alarm_action(cw_actions.SnsAction(alerts_topic))
        unhealthy_alarm.add_ok_action(cw_actions.SnsAction(alerts_topic))

        error_alarm = cloudwatch.Alarm(
            self,
            "Target5xxAlarm",
            alarm_name=f"{construct_id}-alb-5xx",
            metric=alb.metrics.http_code_target(
                elbv2.HttpCodeTarget.TARGET_5XX_COUNT, period=Duration.minutes(5)
            ),
            threshold=5,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="More than 5 HTTP 5xx responses from the app in 5 minutes",
        )
        error_alarm.add_alarm_action(cw_actions.SnsAction(alerts_topic))

        cpu_metric = cloudwatch.Metric(
            namespace="AWS/EC2",
            metric_name="CPUUtilization",
            dimensions_map={"InstanceId": app_instance.instance_id},
            label="CPUUtilization",
            period=Duration.minutes(5),
            statistic="Average",
        )
        cpu_alarm = cloudwatch.Alarm(
            self,
            "CpuAlarm",
            alarm_name=f"{construct_id}-ec2-cpu",
            metric=cpu_metric,
            threshold=70,
            evaluation_periods=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="Sustained CPU above 70% on the application instance",
        )
        cpu_alarm.add_alarm_action(cw_actions.SnsAction(alerts_topic))

        # --- FinOps: budget ------------------------------------------------
        budgets.CfnBudget(
            self,
            "MonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=10, unit="USD"),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type="ACTUAL",
                        threshold=80,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            address=alerts_topic.topic_arn, subscription_type="SNS"
                        )
                    ],
                ),
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type="FORECASTED",
                        threshold=100,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            address=alerts_topic.topic_arn, subscription_type="SNS"
                        )
                    ],
                ),
            ],
        )

        # --- Auditing: CloudTrail -----------------------------------------
        trail_bucket = s3.Bucket(
            self,
            "CloudTrailBucket",
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            auto_delete_objects=True,
            removal_policy=RemovalPolicy.DESTROY,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
        )
        trail_name = f"IncidentTriage-{environment_name}"
        cloudtrail.Trail(
            self,
            "Trail",
            trail_name=trail_name,
            bucket=trail_bucket,
            is_multi_region_trail=False,
            include_global_service_events=True,
            management_events=cloudtrail.ReadWriteType.ALL,
            enable_file_validation=True,
            send_to_cloud_watch_logs=False,
        )
        # Built as a string (rather than trail.trail_arn) so the bucket policy does
        # not create a circular dependency with the trail resource.
        trail_source_arn = (
            f"arn:{cdk.Aws.PARTITION}:cloudtrail:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}"
            f":trail/{trail_name}"
        )

        trail_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AWSCloudTrailAclCheck",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("cloudtrail.amazonaws.com")],
                actions=["s3:GetBucketAcl"],
                resources=[trail_bucket.bucket_arn],
                conditions={"StringEquals": {"aws:SourceArn": trail_source_arn}},
            )
        )
        trail_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AWSCloudTrailWrite",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("cloudtrail.amazonaws.com")],
                actions=["s3:PutObject"],
                resources=[f"{trail_bucket.bucket_arn}/AWSLogs/{cdk.Aws.ACCOUNT_ID}/*"],
                conditions={
                    "StringEquals": {
                        "s3:x-amz-acl": "bucket-owner-full-control",
                        "aws:SourceArn": trail_source_arn,
                    }
                },
            )
        )

        # --- Tags (cost allocation) ---------------------------------------
        Tags.of(self).add("Project", "IncidentTriage")
        Tags.of(self).add("Environment", environment_name)
        Tags.of(self).add("ManagedBy", "AWSCDK")
        Tags.of(self).add("Owner", "justin@zorneo.dev")
        Tags.of(self).add("Component", "api")

        # --- cdk-nag (AwsSolutions) acknowledgements ----------------------
        # Every suppression records why the finding is accepted for a Level 1
        # lab; anything not listed here must be fixed, not suppressed.
        NagSuppressions.add_resource_suppressions(
            vpc,
            [
                NagPackSuppression(
                    id="AwsSolutions-VPC7",
                    reason=(
                        "Lab environment: VPC Flow Logs are billed per GB ingested to CloudWatch "
                        "Logs. Network diagnosis is instead exercised with ALB access logs, "
                        "CloudWatch metrics/alarms and the security-group-by-reference design. "
                        "Flow logging is tracked as a Level 3 hardening item."
                    ),
                )
            ],
        )
        NagSuppressions.add_resource_suppressions(
            alb_sg,
            [
                NagPackSuppression(
                    id="AwsSolutions-EC23",
                    reason=(
                        "The load balancer is intentionally internet-facing and serves a single "
                        "HTTP port (80) - no TLS certificate or domain exists yet (see ADR 0001). "
                        "The application security group accepts traffic only from this security "
                        "group by reference, never from 0.0.0.0/0. HTTPS via ACM is a Level 2 item."
                    ),
                )
            ],
        )
        NagSuppressions.add_resource_suppressions(
            app_instance,
            [
                NagPackSuppression(
                    id="AwsSolutions-EC28",
                    reason=(
                        "Detailed (1-minute) EC2 monitoring doubles the CloudWatch cost of this "
                        "lab instance. Five-minute metrics are sufficient for the CPU alarm and "
                        "the availability alarms are based on ALB metrics, which are unaffected."
                    ),
                ),
                NagPackSuppression(
                    id="AwsSolutions-EC29",
                    reason=(
                        "A single, deliberately simple instance is the whole point of Level 1 "
                        "(understand before scale); there is intentionally no ASG. Recovery is "
                        "demonstrated by recreating the instance from CDK (see the Recovery lab in "
                        "the runbook), so termination protection is left off to permit that lab "
                        "and a clean cdk destroy."
                    ),
                ),
            ],
        )
        NagSuppressions.add_resource_suppressions(
            instance_role,
            [
                NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "Each wildcard is required by the AWS API and is narrowed where the API "
                        "allows it: s3:GetObject/GetBucket*/List* are limited to the CDK assets "
                        "bucket that holds the app bundle; the bedrock inference-profile/* ARN is "
                        "scoped to this account's inference profiles for the triage model; "
                        "logs:DescribeLogGroups and the SSM Session Manager APIs do not support "
                        "resource-level permissions; the bare Resource::* applies to "
                        "cloudwatch:PutMetricData, which is constrained by the "
                        "cloudwatch:namespace condition. DynamoDB and bedrock:InvokeModel are "
                        "scoped to explicit ARNs."
                    ),
                )
            ],
            apply_to_children=True,
        )
        NagSuppressions.add_resource_suppressions(
            alb_logs_bucket,
            [
                NagPackSuppression(
                    id="AwsSolutions-S1",
                    reason=(
                        "This bucket exists to store ALB access logs; it is not read by users or "
                        "applications, so server access logging on the log bucket adds cost with "
                        "no investigative benefit."
                    ),
                )
            ],
        )
        NagSuppressions.add_resource_suppressions(
            trail_bucket,
            [
                NagPackSuppression(
                    id="AwsSolutions-S1",
                    reason=(
                        "This bucket stores CloudTrail audit log files with public access fully "
                        "blocked and TLS enforced. Logging access to the audit-log bucket is "
                        "intentionally out of scope for the lab budget."
                    ),
                )
            ],
        )
        NagSuppressions.add_resource_suppressions(
            table,
            [
                NagPackSuppression(
                    id="AwsSolutions-DDB3",
                    reason=(
                        "The lab table is created with RemovalPolicy.DESTROY and is meant to be "
                        "recreated, so point-in-time recovery adds cost without value. PITR and "
                        "the restore procedure are part of the Level 3 DR plan."
                    ),
                )
            ],
        )

        # --- Outputs -------------------------------------------------------
        CfnOutput(self, "ApplicationUrl", value=f"http://{alb.load_balancer_dns_name}",
                  description="Base URL of the triage API (GET /health, GET /docs)")
        CfnOutput(self, "LoadBalancerDnsName", value=alb.load_balancer_dns_name)
        CfnOutput(self, "TableName", value=table.table_name)
        CfnOutput(self, "LogGroupName", value=app_log_group.log_group_name)
        CfnOutput(self, "AlertsTopicArn", value=alerts_topic.topic_arn)
        CfnOutput(self, "InstanceId", value=app_instance.instance_id)
        CfnOutput(self, "ModelId", value=model_id)
        CfnOutput(
            self,
            "SsmStartSession",
            value=f"aws ssm start-session --target {app_instance.instance_id}",
        )
