from aws_cdk import Stack, CfnOutput, Duration
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_iam as iam
from aws_cdk import aws_ec2 as ec2
from constructs import Construct


class ApiStack(Stack):
    """Public entry point for the frontend: a Lambda Function URL, not API
    Gateway -- a live multi-tool agentic invocation can exceed API Gateway's
    hard, non-configurable 29-second integration timeout. The proxy Lambda's
    only job is bedrock-agentcore:InvokeAgentRuntime; identity is validated
    via cognito-idp:GetUser on the caller's access token.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.Vpc,
        lambda_sg: ec2.SecurityGroup,
        agent_runtime_arn: str,
        agent_runtime_endpoint_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        proxy_fn = lambda_.Function(
            self,
            "ProxyFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/proxy"),
            timeout=Duration.seconds(90),
            memory_size=256,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[lambda_sg],
            environment={
                "AGENT_RUNTIME_ARN": agent_runtime_arn,
                "AGENT_RUNTIME_ENDPOINT_NAME": agent_runtime_endpoint_name,
            },
        )
        proxy_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[
                    f"{agent_runtime_arn}",
                    f"{agent_runtime_arn}/runtime-endpoint/*",
                ],
            )
        )
        proxy_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cognito-idp:GetUser"],
                resources=["*"],
            )
        )

        self.function_url = proxy_fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=["*"],
                allowed_methods=[lambda_.HttpMethod.POST],
                allowed_headers=["authorization", "content-type"],
            ),
        )

        self.proxy_fn = proxy_fn
        CfnOutput(self, "ApiFunctionUrl", value=self.function_url.url)
