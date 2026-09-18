from aws_cdk import Stack, CfnOutput, Duration
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_iam as iam
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_s3 as s3
from constructs import Construct

MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
CHAT_FN_NAME = "PolicyPortal-InsightsChat"


class InsightsApiStack(Stack):
    """Public (no auth -- read-only, no transactions, transparency is the
    point), Lambda-Function-URL-backed chat API for the Class Insights
    companion app. Reuses the main app's existing VPC rather than
    provisioning a second one -- the endpoints it needs (bedrock-runtime,
    logs) already exist there.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.Vpc,
        lambda_sg: ec2.SecurityGroup,
        knowledge_bucket: s3.Bucket,
        allowed_log_groups: list,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Explicit literal function_name (rather than letting CDK generate a
        # token-based physical name) so ALLOWED_LOG_GROUPS can include this
        # function's own log group name as a plain string. Using the token
        # form (chat_fn.function_name) here created a genuine circular
        # dependency -- the function's own Environment property referencing
        # its own logical ID via Ref -- confirmed by `cdk synth` flagging
        # "Circular Dependencies ... ChatFn -> ChatFnServiceRoleDefaultPolicy
        # -> ChatFn". Same bug class as the general "never let a resource's
        # own property reference an attribute of something that depends on
        # it" lesson from prior classes, just turned on itself this time.
        own_log_group = f"/aws/lambda/{CHAT_FN_NAME}"

        chat_fn = lambda_.Function(
            self,
            "ChatFn",
            function_name=CHAT_FN_NAME,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/insights_chat"),
            timeout=Duration.seconds(60),
            memory_size=256,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[lambda_sg],
            environment={
                "MODEL_ID": MODEL_ID,
                "KNOWLEDGE_BUCKET": knowledge_bucket.bucket_name,
                "KNOWLEDGE_KEY": "projectknowledge.md",
                "ALLOWED_LOG_GROUPS": ",".join(allowed_log_groups + [own_log_group]),
            },
        )

        knowledge_bucket.grant_read(chat_fn)

        chat_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream"],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
                ],
            )
        )

        # Real security boundary: explicit allow-listed log group ARNs, not a
        # wildcard. Includes this function's own log group so students can
        # ask about the Insights app's own activity too.
        allowed_arns = [
            f"arn:aws:logs:{self.region}:{self.account}:log-group:{lg}:*"
            for lg in allowed_log_groups + [own_log_group]
        ]
        chat_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["logs:FilterLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams"],
                resources=allowed_arns,
            )
        )

        self.function_url = chat_fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=["*"],
                allowed_methods=[lambda_.HttpMethod.POST],
                allowed_headers=["content-type"],
            ),
        )

        self.chat_fn = chat_fn
        CfnOutput(self, "InsightsApiUrl", value=self.function_url.url)
