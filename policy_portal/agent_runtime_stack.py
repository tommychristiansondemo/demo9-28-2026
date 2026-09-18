import boto3
from aws_cdk import Stack, CfnOutput, Duration, CustomResource
from aws_cdk import aws_bedrockagentcore as bac
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_s3 as s3
from aws_cdk import custom_resources as cr
from constructs import Construct

MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
RUNTIME_NAME = "policy_portal_agent"
ENDPOINT_NAME = "prod"
AGENT_CODE_KEY = "agent-code/policy-agent.zip"


class AgentRuntimeStack(Stack):
    """AgentCore Runtime running the agent's manual Converse tool-use loop,
    direct-code (S3 zip) deploy -- no ECR/Docker. Must be deployed AFTER
    scripts/package_agent.sh has uploaded a real zip to agent_code_bucket,
    same 'populate before the Runtime references it' ordering used in prior
    classes for their ECR-image equivalent.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.Vpc,
        lambda_sg: ec2.SecurityGroup,
        agent_code_bucket: s3.Bucket,
        agent_code_bucket_name: str,
        gateway_url: str,
        gateway_arn: str,
        knowledge_base_id: str,
        guardrail_id: str,
        guardrail_version: str,
        memory_id: str,
        memory_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        s3_client = boto3.client("s3", region_name=self.region)
        try:
            head = s3_client.head_object(Bucket=agent_code_bucket_name, Key=AGENT_CODE_KEY)
            code_version_id = head["VersionId"]
        except Exception as e:
            raise RuntimeError(
                f"Agent code not found at s3://{agent_code_bucket_name}/{AGENT_CODE_KEY}. "
                "Run scripts/package_agent.sh BEFORE deploying this stack."
            ) from e

        runtime_role = iam.Role(
            self,
            "RuntimeRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream"],
                resources=[
                    # Cross-region inference profiles (the "us." prefix) route
                    # Converse calls to underlying foundation models in OTHER
                    # regions than the caller's own -- confirmed live via a
                    # real AccessDeniedException naming a us-east-1 resource
                    # while this stack runs in us-east-2. foundation-model
                    # ARNs carry no account ID, so wildcarding the region is
                    # the standard, safe way to grant this.
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
                ],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:ApplyGuardrail"],
                resources=[f"arn:aws:bedrock:{self.region}:{self.account}:guardrail/{guardrail_id}"],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:Retrieve"],
                resources=[f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/{knowledge_base_id}"],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeGateway", "bedrock-agentcore:*"],
                resources=[gateway_arn],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogGroups", "logs:DescribeLogStreams"],
                resources=["*"],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface"],
                resources=["*"],
            )
        )
        agent_code_bucket.grant_read(runtime_role)
        # AgentCore Memory: real security boundary scoped to this one
        # memory resource's ARN, not a wildcard. CreateEvent/ListEvents
        # give the agent read/write of a session's short-term (raw) turns;
        # RetrieveMemoryRecords/GetEvent give it read access to the
        # extracted long-term per-user summary.
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                ],
                resources=[memory_arn],
            )
        )

        self.runtime = bac.CfnRuntime(
            self,
            "Runtime",
            agent_runtime_name=RUNTIME_NAME,
            role_arn=runtime_role.role_arn,
            description="Policy & Benefits Assistant agent -- RAG + mock leave/benefits tools + Guardrail.",
            agent_runtime_artifact=bac.CfnRuntime.AgentRuntimeArtifactProperty(
                code_configuration=bac.CfnRuntime.CodeConfigurationProperty(
                    code=bac.CfnRuntime.CodeProperty(
                        s3=bac.CfnRuntime.S3LocationProperty(
                            bucket=agent_code_bucket.bucket_name,
                            prefix=AGENT_CODE_KEY,
                            version_id=code_version_id,
                        ),
                    ),
                    entry_point=["agent.py"],
                    runtime="PYTHON_3_12",
                ),
            ),
            network_configuration=bac.CfnRuntime.NetworkConfigurationProperty(
                network_mode="VPC",
                network_mode_config=bac.CfnRuntime.VpcConfigProperty(
                    security_groups=[lambda_sg.security_group_id],
                    subnets=vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED).subnet_ids,
                ),
            ),
            environment_variables={
                "MODEL_ID": MODEL_ID,
                "GATEWAY_URL": gateway_url,
                "KNOWLEDGE_BASE_ID": knowledge_base_id,
                "GUARDRAIL_ID": guardrail_id,
                "GUARDRAIL_VERSION": guardrail_version,
                "MEMORY_ID": memory_id,
                "AWS_REGION_NAME": self.region,
            },
        )

        waiter_fn = lambda_.Function(
            self,
            "RuntimeWaiterFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/runtime_waiter"),
            timeout=Duration.minutes(8),
        )
        waiter_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:GetAgentRuntime"],
                resources=["*"],
            )
        )
        provider = cr.Provider(self, "RuntimeWaiterProvider", on_event_handler=waiter_fn)
        waiter_cr = CustomResource(
            self,
            "RuntimeWaiter",
            service_token=provider.service_token,
            properties={
                "AgentRuntimeId": self.runtime.attr_agent_runtime_id,
                # Included so the waiter custom resource actually re-runs
                # (and blocks the endpoint update below) on every new agent
                # code version, not just on first create.
                "AgentRuntimeVersion": self.runtime.attr_agent_runtime_version,
            },
        )
        waiter_cr.node.add_dependency(self.runtime)

        self.endpoint = bac.CfnRuntimeEndpoint(
            self,
            "RuntimeEndpoint",
            agent_runtime_id=self.runtime.attr_agent_runtime_id,
            name=ENDPOINT_NAME,
            # Without this, CloudFormation sees no property diff on the
            # endpoint when only the Runtime's code changes, so a redeploy
            # updates the Runtime to a new version but never moves the
            # "prod" endpoint's live traffic to it -- confirmed live: the
            # endpoint stayed on version 1 while the Runtime moved to
            # version 2 until this was added. Same bug class as the
            # UpdateAgentRuntime/UpdateAgentRuntimeEndpoint race documented
            # in a prior class's ModelSelector feature.
            agent_runtime_version=self.runtime.attr_agent_runtime_version,
        )
        self.endpoint.node.add_dependency(waiter_cr)

        self.runtime_role = runtime_role

        CfnOutput(self, "AgentRuntimeId", value=self.runtime.attr_agent_runtime_id)
        CfnOutput(self, "AgentRuntimeArn", value=self.runtime.attr_agent_runtime_arn)
        CfnOutput(self, "AgentRuntimeEndpointName", value=ENDPOINT_NAME)
