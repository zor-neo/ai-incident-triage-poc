"""Level 2 - Delivery Engineer.

Containerised delivery: the same FastAPI service, but built into an immutable
image, stored in ECR, and run on ECS Fargate as two independent environments
(staging and prod) behind one internet-facing ALB.

Two stacks on purpose (technical spec section 6.2):

  IncidentTriage-<env>-registry  ECR only. It exists as its own stack because the
                                 registry must exist *before* the first image can
                                 be pushed, and the image must exist *before* an
                                 ECS service can reach a steady state - otherwise
                                 the first ``cdk deploy`` waits on a service that
                                 can never stabilise. Splitting the registry makes
                                 that ordering explicit instead of hiding it.

  IncidentTriage-<env>           Everything else: network, cluster, two services,
                                 two task definitions, ALB, alarms, and the
                                 GitHub OIDC deploy role.

IAM separation is the point of this level. Three roles, none interchangeable:

  * execution role - used by the *ECS agent*: pull the image, write logs, read the
    one SSM SecureString secret. No DynamoDB, no Bedrock.
  * task role      - used by *the application*: DynamoDB on its own table,
    bedrock:InvokeModel on one model, PutMetricData in one namespace. It cannot
    pull images, read logs, or read the secret it was started with.
  * deploy role    - assumed by GitHub Actions via OIDC (no long-lived keys):
    push images and move a service between immutable revisions. It cannot read
    application data at all.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subscriptions,
    aws_ssm as ssm,
)
from constructs import Construct
from cdk_nag import NagPackSuppression, NagSuppressions

GITHUB_OIDC_URL = "token.actions.githubusercontent.com"
LOG_GROUP_NAME = "/aws/ecs/incident-triage"

# Environment matrix. One entry per ECS service; they differ only in name, data
# table, and which ALB listener port fronts them.
ENVIRONMENTS: list[dict] = [
    {"name": "staging", "listener_port": 8080, "desired_count": 1},
    {"name": "prod", "listener_port": 80, "desired_count": 1},
]


class Level2RegistryStack(Stack):
    """The ECR repository, deployed before any image is pushed."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment_name: str,
        repository_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        repository = ecr.Repository(
            self,
            "Repository",
            repository_name=repository_name,
            # Immutable tags are what make "deploy the commit SHA" meaningful: a
            # tag can never be repointed at different bytes after the fact.
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            image_scan_on_push=True,
            encryption=ecr.RepositoryEncryption.AES_256,
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
            lifecycle_rules=[
                ecr.LifecycleRule(
                    description="Expire untagged images (failed builds, superseded digests)",
                    tag_status=ecr.TagStatus.UNTAGGED,
                    max_image_age=Duration.days(1),
                ),
                ecr.LifecycleRule(
                    description="Keep the last 30 releases",
                    max_image_count=30,
                ),
            ],
        )

        Tags.of(self).add("Project", "IncidentTriage")
        Tags.of(self).add("Environment", environment_name)
        Tags.of(self).add("ManagedBy", "AWSCDK")
        Tags.of(self).add("Owner", "justin@zorneo.dev")
        Tags.of(self).add("Component", "registry")

        CfnOutput(self, "RepositoryUri", value=repository.repository_uri)
        CfnOutput(self, "RepositoryName", value=repository.repository_name)
        CfnOutput(
            self,
            "PushImage",
            value=(
                f"docker build --platform linux/amd64 -t {repository.repository_uri}:$SHA . && "
                f"docker push {repository.repository_uri}:$SHA"
            ),
            description="Build and push the image tagged with the commit SHA",
        )


class Level2EcsStack(Stack):
    """Network, cluster, two Fargate services, ALB, alarms, and the deploy role."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment_name: str,
        model_id: str,
        git_sha: str,
        image_tag: str,
        repository_name: str,
        github_repo: str,
        github_branch: str,
        alert_email: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.environment_name = environment_name
        self.model_id = model_id
        self.git_sha = git_sha

        repository = ecr.Repository.from_repository_name(
            self, "Repository", repository_name
        )

        # --- Network ------------------------------------------------------
        vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr("10.30.0.0/16"),
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
            # Table traffic stays on the AWS network and off the NAT gateway.
            # ECR layer downloads also take this path and are charged as S3
            # traffic rather than NAT-processed traffic.
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

        # --- Observability: one log group, one stream prefix per environment --
        app_log_group = logs.LogGroup(
            self,
            "AppLogGroup",
            log_group_name=LOG_GROUP_NAME,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Alerts -------------------------------------------------------
        alerts_topic = sns.Topic(
            self,
            "AlertsTopic",
            topic_name=f"IncidentTriage-alerts-{environment_name}",
            display_name="AI Incident Triage - Level 2 operational alerts",
            enforce_ssl=True,
        )
        if alert_email:
            alerts_topic.add_subscription(sns_subscriptions.EmailSubscription(alert_email))

        # --- Security groups (by reference, as in Level 1) ----------------
        alb_sg = ec2.SecurityGroup(
            self,
            "AlbSecurityGroup",
            vpc=vpc,
            description="Public HTTP ingress for the Level 2 load balancer",
            allow_all_outbound=False,
        )
        for port in (80, 8080):
            alb_sg.add_ingress_rule(
                ec2.Peer.any_ipv4(),
                ec2.Port.tcp(port),
                f"HTTP {port} from the internet (lab environment: no TLS certificate yet)",
            )
        alb_sg.add_egress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS to AWS APIs if required by the ALB"
        )

        task_sg = ec2.SecurityGroup(
            self,
            "TaskSecurityGroup",
            vpc=vpc,
            description="FastAPI traffic from the ALB security group only, by reference",
            allow_all_outbound=False,
        )
        task_sg.add_ingress_rule(
            alb_sg, ec2.Port.tcp(8000), "FastAPI from the ALB security group (reference)"
        )
        alb_sg.add_egress_rule(task_sg, ec2.Port.tcp(8000), "Forward requests to tasks")
        task_sg.add_egress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(443),
            "HTTPS to AWS APIs (Bedrock, ECR, CloudWatch, SSM)",
        )
        task_sg.add_egress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.udp(53), "DNS via the VPC resolver"
        )

        # --- Cluster ------------------------------------------------------
        cluster = ecs.Cluster(
            self,
            "Cluster",
            cluster_name=f"incident-triage-{environment_name}",
            vpc=vpc,
            # Container Insights is a paid, Level 3 observability item; keep it
            # explicitly off so the cost is a decision, not a default.
            container_insights=False,
        )

        # --- Load balancer -----------------------------------------------
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

        outputs: dict[str, dict[str, str]] = {}
        execution_roles: list[iam.Role] = []
        task_roles: list[iam.Role] = []
        service_names: list[str] = []

        # --- One table + task definition + service per environment --------
        for env in ENVIRONMENTS:
            env_name: str = env["name"]

            table = dynamodb.Table(
                self,
                f"IncidentTable-{env_name}",
                table_name=f"IncidentTriage-{env_name}",
                partition_key=dynamodb.Attribute(
                    name="incident_id", type=dynamodb.AttributeType.STRING
                ),
                billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
                encryption=dynamodb.TableEncryption.AWS_MANAGED,
                removal_policy=RemovalPolicy.DESTROY,
                deletion_protection=False,
            )

            # The secret is NOT created here and its value is never in this
            # template: it is created out of band (scripts/level2_bootstrap.sh)
            # and imported by name, so neither CloudFormation nor the pipeline
            # ever holds the plaintext.
            secret_param = ssm.StringParameter.from_secure_string_parameter_attributes(
                self,
                f"InternalApiKeyParam-{env_name}",
                parameter_name=f"/incident-triage/{environment_name}/{env_name}/internal-api-key",
            )

            # --- Execution role: ECS agent only ---------------------------
            execution_role = iam.Role(
                self,
                f"TaskExecutionRole-{env_name}",
                assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
                description=(
                    f"ECS agent role for {env_name}: pull image, write logs, read one secret"
                ),
            )
            repository.grant_pull(execution_role)
            secret_param.grant_read(execution_role)
            app_log_group.grant_write(execution_role)
            # SecureString decryption uses the AWS-managed aws/ssm key, which has
            # no stable cross-account ARN to scope to; kms:ViaService is the AWS
            # documented constraint for exactly this case.
            execution_role.add_to_policy(
                iam.PolicyStatement(
                    sid="DecryptSsmSecureStringViaSsmOnly",
                    actions=["kms:Decrypt"],
                    resources=["*"],
                    conditions={
                        "StringEquals": {
                            "kms:ViaService": f"ssm.{cdk.Aws.REGION}.amazonaws.com"
                        }
                    },
                )
            )
            NagSuppressions.add_resource_suppressions(
                execution_role,
                [
                    NagPackSuppression(
                        id="AwsSolutions-IAM5",
                        applies_to=["Resource::*"],
                        reason=(
                            "kms:Decrypt is required for the ECS agent to resolve an SSM "
                            "SecureString. The key is the AWS-managed alias/aws/ssm, which has "
                            "no account-stable ARN to name in a Resource, so the AWS-documented "
                            "constraint is used instead: kms:ViaService is pinned to this "
                            "region's SSM endpoint. Log writes are scoped to the one log group "
                            "and image pull to the one repository."
                        ),
                    )
                ],
                apply_to_children=True,
            )

            # --- Task role: the application only --------------------------
            task_role = iam.Role(
                self,
                f"TaskRole-{env_name}",
                assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
                description=(
                    f"Application role for {env_name}: one table, one model, one metric namespace"
                ),
            )
            table.grant(task_role, "dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem")
            task_role.add_to_policy(
                iam.PolicyStatement(
                    sid="InvokeTriageModel",
                    actions=["bedrock:InvokeModel"],
                    resources=[
                        f"arn:{cdk.Aws.PARTITION}:bedrock:{cdk.Aws.REGION}::foundation-model/{model_id}",
                        f"arn:{cdk.Aws.PARTITION}:bedrock:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}:inference-profile/*",
                    ],
                )
            )
            task_role.add_to_policy(
                iam.PolicyStatement(
                    sid="PublishCustomMetrics",
                    actions=["cloudwatch:PutMetricData"],
                    resources=["*"],
                    conditions={"StringEquals": {"cloudwatch:namespace": "IncidentTriage"}},
                )
            )

            # --- Task definition ------------------------------------------
            task_definition = ecs.FargateTaskDefinition(
                self,
                f"TaskDefinition-{env_name}",
                family=f"incident-triage-{env_name}",
                cpu=256,
                memory_limit_mib=512,
                runtime_platform=ecs.RuntimePlatform(
                    cpu_architecture=ecs.CpuArchitecture.X86_64,
                    operating_system_family=ecs.OperatingSystemFamily.LINUX,
                ),
                execution_role=execution_role,
                task_role=task_role,
            )
            NagSuppressions.add_resource_suppressions(
                task_definition,
                [
                    NagPackSuppression(
                        id="AwsSolutions-ECS2",
                        reason=(
                            "Every value passed via `environment` is non-secret runtime "
                            "configuration (environment name, region, table name, model id, git "
                            "sha, log level, metric namespace) and is deliberately visible in "
                            "the template so a reviewer can see exactly what the task runs with. "
                            "The application's one secret, INTERNAL_API_KEY, is NOT here: it is "
                            "injected through `secrets` from an SSM SecureString at task start."
                        ),
                    )
                ],
            )

            container = task_definition.add_container(
                "app",
                container_name="app",
                image=ecs.ContainerImage.from_ecr_repository(repository, tag=image_tag),
                essential=True,
                # Non-secret runtime configuration. The secret arrives separately
                # through `secrets` below, resolved by the ECS agent from SSM.
                environment={
                    "ENVIRONMENT": env_name,
                    "AWS_REGION": self.region,
                    "AWS_DEFAULT_REGION": self.region,
                    "TABLE_NAME": table.table_name,
                    "BEDROCK_MODEL_ID": model_id,
                    "GIT_SHA": git_sha,
                    "LOG_LEVEL": "INFO",
                    "METRICS_NAMESPACE": "IncidentTriage",
                },
                secrets={
                    "INTERNAL_API_KEY": ecs.Secret.from_ssm_parameter(secret_param),
                },
                logging=ecs.LogDrivers.aws_logs(
                    stream_prefix=env_name,
                    log_group=app_log_group,
                ),
                health_check=ecs.HealthCheck(
                    command=[
                        "CMD-SHELL",
                        "python -c \"import sys,urllib.request; "
                        "sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',"
                        "timeout=2).status == 200 else 1)\"",
                    ],
                    interval=Duration.seconds(30),
                    timeout=Duration.seconds(5),
                    retries=3,
                    start_period=Duration.seconds(10),
                ),
                stop_timeout=Duration.seconds(10),
            )
            container.add_port_mappings(ecs.PortMapping(container_port=8000))

            service = ecs.FargateService(
                self,
                f"Service-{env_name}",
                service_name=f"incident-triage-{env_name}",
                cluster=cluster,
                task_definition=task_definition,
                desired_count=env["desired_count"],
                security_groups=[task_sg],
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                assign_public_ip=False,
                # Circuit breaker / automatic rollback are Level 3 ("controlled
                # change"). At this level a bad release is caught by the alarm and
                # reversed by an explicit, auditable rollback to the previous
                # immutable revision - see docs/runbooks/level2-runbook.md.
                circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=False),
                min_healthy_percent=100,
                max_healthy_percent=200,
            )

            target_group = elbv2.ApplicationTargetGroup(
                self,
                f"TargetGroup-{env_name}",
                target_group_name=f"incident-triage-{env_name}",
                vpc=vpc,
                port=8000,
                protocol=elbv2.ApplicationProtocol.HTTP,
                targets=[service],
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
                f"Listener-{env_name}",
                port=int(env["listener_port"]),
                protocol=elbv2.ApplicationProtocol.HTTP,
                default_target_groups=[target_group],
            )

            unhealthy_alarm = cloudwatch.Alarm(
                self,
                f"UnhealthyTargetsAlarm-{env_name}",
                alarm_name=f"{construct_id}-{env_name}-alb-unhealthy-targets",
                metric=target_group.metrics.unhealthy_host_count(period=Duration.minutes(1)),
                threshold=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                evaluation_periods=1,
                datapoints_to_alarm=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description=f"{env_name}: one or more ECS tasks failed the /health check",
            )
            unhealthy_alarm.add_alarm_action(cw_actions.SnsAction(alerts_topic))
            unhealthy_alarm.add_ok_action(cw_actions.SnsAction(alerts_topic))

            error_alarm = cloudwatch.Alarm(
                self,
                f"Target5xxAlarm-{env_name}",
                alarm_name=f"{construct_id}-{env_name}-alb-5xx",
                metric=alb.metrics.http_code_target(
                    elbv2.HttpCodeTarget.TARGET_5XX_COUNT, period=Duration.minutes(5)
                ),
                threshold=5,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description=f"{env_name}: more than 5 HTTP 5xx responses in 5 minutes",
            )
            error_alarm.add_alarm_action(cw_actions.SnsAction(alerts_topic))

            outputs[env_name] = {
                "table": table.table_name,
                "service": service.service_name,
                "task_definition_family": task_definition.family,
                "target_group": target_group.target_group_name,
                "task_role": task_role.role_arn,
                "port": str(env["listener_port"]),
                "url": f"http://{alb.load_balancer_dns_name}"
                + ("" if str(env["listener_port"]) == "80" else f":{env['listener_port']}"),
            }
            execution_roles.append(execution_role)
            task_roles.append(task_role)
            service_names.append(service.service_name)
            self._suppress_nag(table, task_role)

        # --- GitHub OIDC deploy role -------------------------------------
        # The provider already exists in this account (created for an earlier
        # project), so it is imported rather than created - creating a second one
        # for the same URL fails with EntityAlreadyExists.
        provider = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(
            self,
            "GithubOidcProvider",
            f"arn:{cdk.Aws.PARTITION}:iam::{cdk.Aws.ACCOUNT_ID}:oidc-provider/{GITHUB_OIDC_URL}",
        )

        deploy_role = iam.Role(
            self,
            "GithubDeployRole",
            role_name=f"incident-triage-{environment_name}-github-deploy",
            assumed_by=iam.WebIdentityPrincipal(
                provider.open_id_connect_provider_arn,
                conditions={
                    "StringEquals": {
                        f"{GITHUB_OIDC_URL}:aud": "sts.amazonaws.com",
                    },
                    # Scoped to this repository and to exactly two subjects: a
                    # push to the deploy branch, and the protected GitHub
                    # Environment that gates production. Anything else - a fork,
                    # another repo, another branch, a PR from a fork - cannot
                    # assume this role. An unrestricted `sub` is the documented
                    # way GitHub OIDC goes wrong.
                    "StringLike": {
                        f"{GITHUB_OIDC_URL}:sub": [
                            f"repo:{github_repo}:ref:refs/heads/{github_branch}",
                            f"repo:{github_repo}:environment:production",
                        ]
                    },
                },
            ),
            description="Assumed by GitHub Actions via OIDC to push images and roll a service",
        )
        deploy_role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrLogin",
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        deploy_role.add_to_policy(
            iam.PolicyStatement(
                sid="PushAndScanImages",
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload",
                    "ecr:PutImage",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:DescribeImages",
                    "ecr:DescribeImageScanFindings",
                ],
                resources=[repository.repository_arn],
            )
        )
        deploy_role.add_to_policy(
            iam.PolicyStatement(
                sid="RollServices",
                actions=["ecs:UpdateService", "ecs:DescribeServices"],
                resources=[
                    f"arn:{cdk.Aws.PARTITION}:ecs:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}:"
                    f"service/{cluster.cluster_name}/{name}"
                    for name in service_names
                ],
            )
        )
        # RegisterTaskDefinition and DescribeTaskDefinition do not support
        # resource-level permissions (documented exception), so these two are on *.
        deploy_role.add_to_policy(
            iam.PolicyStatement(
                sid="RegisterTaskDefinition",
                actions=["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition"],
                resources=["*"],
            )
        )
        deploy_role.add_to_policy(
            iam.PolicyStatement(
                sid="PassOnlyTheseFourRoles",
                actions=["iam:PassRole"],
                resources=[
                    role.role_arn for role in [*task_roles, *execution_roles]
                ],
                conditions={"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
            )
        )

        # --- Tags (cost allocation) ---------------------------------------
        Tags.of(self).add("Project", "IncidentTriage")
        Tags.of(self).add("Environment", environment_name)
        Tags.of(self).add("ManagedBy", "AWSCDK")
        Tags.of(self).add("Owner", "justin@zorneo.dev")
        Tags.of(self).add("Component", "delivery")

        # --- cdk-nag acknowledgements -------------------------------------
        NagSuppressions.add_resource_suppressions(
            vpc,
            [
                NagPackSuppression(
                    id="AwsSolutions-VPC7",
                    reason=(
                        "VPC Flow Logs are billed per GB ingested. Network behaviour is "
                        "evidenced instead by ALB access logs, task/ALB metrics and the "
                        "security-group-by-reference design. Flow logging is a Level 3 item."
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
                        "The ALB is intentionally internet-facing and serves two plain HTTP "
                        "ports (80 for prod, 8080 for staging) because no certificate or domain "
                        "exists yet (ADR 0004). Tasks accept traffic only from this security "
                        "group by reference. HTTPS via ACM is a Level 2/3 follow-up."
                    ),
                )
            ],
        )
        NagSuppressions.add_resource_suppressions(
            alb_logs_bucket,
            [
                NagPackSuppression(
                    id="AwsSolutions-S1",
                    reason=(
                        "This bucket exists to hold ALB access logs; it is not read by users or "
                        "applications, so server access logging on it adds cost with no "
                        "investigative benefit."
                    ),
                )
            ],
        )
        NagSuppressions.add_resource_suppressions(
            deploy_role,
            [
                NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    applies_to=["Resource::*"],
                    reason=(
                        "ecr:GetAuthorizationToken is an account-level API with no resource "
                        "ARN, and ecs:RegisterTaskDefinition/DescribeTaskDefinition are "
                        "documented as not supporting resource-level permissions; both are on "
                        "* and nothing else is. Image push is scoped to this one repository, "
                        "service roll is scoped to these two services, and iam:PassRole is "
                        "limited to the two task roles and two execution roles it may hand to "
                        "ecs-tasks.amazonaws.com."
                    ),
                )
            ],
            apply_to_children=True,
        )
        NagSuppressions.add_resource_suppressions(
            cluster,
            [
                NagPackSuppression(
                    id="AwsSolutions-ECS4",
                    reason=(
                        "Container Insights is deliberately disabled: it is a per-metric "
                        "charge and the observability story at this level is the ALB/task "
                        "metrics plus structured JSON logs. Enabled in Level 3."
                    ),
                )
            ],
        )

        # --- Outputs -------------------------------------------------------
        for env_name, info in outputs.items():
            label = env_name.capitalize()
            # The URL must come from the per-environment entry, not from the loop
            # variable - an earlier version read `env["listener_port"]` after the
            # loop had finished, so the staging URL was emitted with prod's port.
            CfnOutput(self, f"ServiceName{label}", value=info["service"])
            CfnOutput(self, f"TaskDefinitionFamily{label}", value=info["task_definition_family"])
            CfnOutput(self, f"TableName{label}", value=info["table"])
            CfnOutput(
                self,
                f"{label}Url",
                value=info["url"],
                description=f"Base URL for the {env_name} environment",
            )
        CfnOutput(self, "LoadBalancerDnsName", value=alb.load_balancer_dns_name)
        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(self, "RepositoryName", value=repository.repository_name)
        CfnOutput(self, "GithubDeployRoleArn", value=deploy_role.role_arn)
        CfnOutput(self, "AlertsTopicArn", value=alerts_topic.topic_arn)
        CfnOutput(self, "LogGroupName", value=app_log_group.log_group_name)

    @staticmethod
    def _suppress_nag(table: dynamodb.Table, task_role: iam.Role) -> None:
        NagSuppressions.add_resource_suppressions(
            table,
            [
                NagPackSuppression(
                    id="AwsSolutions-DDB3",
                    reason=(
                        "Lab table with RemovalPolicy.DESTROY that is meant to be recreated, so "
                        "point-in-time recovery adds cost without value. PITR and the restore "
                        "procedure are part of the Level 3 DR plan."
                    ),
                )
            ],
        )
        NagSuppressions.add_resource_suppressions(
            task_role,
            [
                NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "The only wildcards are the ones the API forces: the per-account "
                        "inference-profile/* ARN (scoped to this account's profiles for the "
                        "triage model) and cloudwatch:PutMetricData, which does not support "
                        "resource-level permissions and is constrained by the "
                        "cloudwatch:namespace condition. DynamoDB and bedrock:InvokeModel are "
                        "scoped to explicit ARNs, and the task role deliberately has no ECR, "
                        "logs or SSM permissions."
                    ),
                )
            ],
            apply_to_children=True,
        )
