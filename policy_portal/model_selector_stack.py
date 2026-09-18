import json

from aws_cdk import Stack, CfnOutput, Duration
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_iam as iam
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_appconfig as appconfig
from aws_cdk import aws_s3 as s3
from constructs import Construct

CHAT_FN_NAME = "PolicyPortal-ModelSelector"


class ModelSelectorStack(Stack):
    """'Bedrock flexibility' demo: AppConfig-stored quality/cost/latency
    weights, a Lambda that scores 5 candidate models against them (4 static
    illustrative scores, 1 -- latency -- measured live), and, on request,
    actually redirects the live AgentCore Runtime to a different model.

    Deliberately NOT placed in the main VPC: the control-plane client this
    Lambda needs (`bedrock-agentcore-control`, for UpdateAgentRuntime) has no
    CDK-exposed PrivateLink endpoint constant, and the existing
    lambdas/runtime_waiter/handler.py already establishes the precedent of
    calling this same client from a non-VPC Lambda in this exact stack set --
    Lambdas outside the customer VPC reach AWS public endpoints directly, no
    NAT/endpoint needed. This is a control/admin action Lambda, not one that
    touches student PII, so skipping VPC isolation here (unlike every
    user-facing Lambda in this app) is a deliberate, narrow exception.

    Fully independent stack (no inbound dependency from PolicyPortal-Api or
    PolicyPortal-AgentRuntime) specifically to avoid the CDK dependency-cycle
    class of bug hit twice already in this series: this stack only ever
    IMPORTS from AgentRuntimeStack (the Runtime's id/endpoint name), never
    the reverse, and owns its own Function URL rather than reaching into
    ApiStack's existing one.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        runtime_id: str,
        runtime_arn: str,
        runtime_role_arn: str,
        agent_code_bucket: s3.Bucket,
        endpoint_name: str,
        default_model_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        state_table = ddb.TableV2(
            self,
            "StateTable",
            partition_key=ddb.Attribute(name="id", type=ddb.AttributeType.STRING),
            billing=ddb.Billing.on_demand(),
        )

        app_config_app = appconfig.CfnApplication(self, "App", name="PolicyPortal-ModelTuning")

        # NOTE: `self.environment` collides with CDK's own Stack base class
        # read-only `environment` property -- confirmed failure in a prior
        # class's identical feature. Use a distinct attribute name.
        self.appconfig_environment = appconfig.CfnEnvironment(
            self, "Env", application_id=app_config_app.ref, name="prod",
        )

        profile = appconfig.CfnConfigurationProfile(
            self, "Profile",
            application_id=app_config_app.ref,
            name="weights",
            location_uri="hosted",
        )

        # Custom deployment strategy with zero final bake time, used for
        # BOTH the one-time seed below and every future weights-save.
        # AppConfig's predefined AllAtOnce/RolloutStrategy.ALL_AT_ONCE
        # carries a hidden 10-minute FinalBakeTimeInMinutes (only visible via
        # get-deployment-strategy, not list-deployment-strategies) that
        # blocks a second deployment to the same environment within that
        # window -- confirmed in a prior class's identical feature. Using the
        # zero-bake strategy from the start (rather than switching later)
        # avoids that prior class's extra complication of needing two
        # strategies, since changing an already-deployed CfnDeployment's
        # strategy forces a replacement.
        strategy = appconfig.CfnDeploymentStrategy(
            self, "ZeroBakeStrategy",
            name="PolicyPortal-ZeroBake",
            deployment_duration_in_minutes=0,
            growth_factor=100,
            replicate_to="NONE",
            final_bake_time_in_minutes=0,
        )

        default_weights = {"quality": 50, "cost": 25, "latency": 25}
        seed_version = appconfig.CfnHostedConfigurationVersion(
            self, "SeedVersion",
            application_id=app_config_app.ref,
            configuration_profile_id=profile.ref,
            content=json.dumps(default_weights),
            content_type="application/json",
        )
        seed_deployment = appconfig.CfnDeployment(
            self, "SeedDeployment",
            application_id=app_config_app.ref,
            environment_id=self.appconfig_environment.ref,
            deployment_strategy_id=strategy.ref,
            configuration_profile_id=profile.ref,
            configuration_version=seed_version.ref,
        )
        seed_deployment.node.add_dependency(self.appconfig_environment)
        seed_deployment.node.add_dependency(strategy)

        fn = lambda_.Function(
            self,
            "ModelSelectorFn",
            function_name=CHAT_FN_NAME,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/model_selector"),
            timeout=Duration.minutes(8),
            memory_size=512,
            environment={
                "RUNTIME_ID": runtime_id,
                "ENDPOINT_NAME": endpoint_name,
                "DEFAULT_MODEL_ID": default_model_id,
                "STATE_TABLE": state_table.table_name,
                "APPCONFIG_APPLICATION_ID": app_config_app.ref,
                "APPCONFIG_ENVIRONMENT_ID": self.appconfig_environment.ref,
                "APPCONFIG_PROFILE_ID": profile.ref,
                "APPCONFIG_STRATEGY_ID": strategy.ref,
            },
        )

        state_table.grant_read_write_data(fn)

        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream"],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
                ],
            )
        )

        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:GetAgentRuntime",
                    "bedrock-agentcore:UpdateAgentRuntime",
                    "bedrock-agentcore:GetAgentRuntimeEndpoint",
                    "bedrock-agentcore:UpdateAgentRuntimeEndpoint",
                ],
                resources=[runtime_arn, f"{runtime_arn}/runtime-endpoint/*"],
            )
        )

        # UpdateAgentRuntime re-supplies the Runtime's role_arn on every
        # call (the API has no partial-patch mode -- see docstring above),
        # which means the CALLER (this Lambda) must itself be allowed to
        # pass that role. Confirmed live: AccessDeniedException on
        # iam:PassRole was the very next error after fixing the AppConfig
        # grant above.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[runtime_role_arn],
            )
        )

        # UpdateAgentRuntime validates the referenced S3 code object using
        # the CALLING principal's own credentials, not just the passed
        # role_arn's -- confirmed live: "Access denied when trying to
        # retrieve zip file from S3" from THIS Lambda's role, even though
        # the runtime_role already has read access and the object is
        # unchanged. Defense-in-depth / anti-confused-deputy check on AWS's
        # side. Grant this Lambda its own read access too.
        agent_code_bucket.grant_read(fn)

        # appconfig:StartDeployment needs a grant on the deployment
        # strategy's OWN arn, a separate resource from the application --
        # scoping only to application/<id> fails live with
        # AccessDeniedException naming the missing resource (confirmed in a
        # prior class's identical feature).
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "appconfig:CreateHostedConfigurationVersion",
                    "appconfig:StartDeployment",
                    "appconfig:GetDeployment",
                ],
                resources=[
                    # AccessDeniedException confirmed live that the action
                    # authorizes against the bare application ARN (no
                    # trailing path), not just a wildcarded sub-resource --
                    # grant both to be safe.
                    f"arn:aws:appconfig:{self.region}:{self.account}:application/{app_config_app.ref}",
                    f"arn:aws:appconfig:{self.region}:{self.account}:application/{app_config_app.ref}/*",
                    f"arn:aws:appconfig:{self.region}:{self.account}:deploymentstrategy/{strategy.ref}",
                ],
            )
        )

        self.function_url = fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=["*"],
                allowed_methods=[lambda_.HttpMethod.GET, lambda_.HttpMethod.POST],
                allowed_headers=["content-type"],
            ),
        )

        self.fn = fn
        CfnOutput(self, "ModelSelectorUrl", value=self.function_url.url)
        CfnOutput(self, "AppConfigApplicationId", value=app_config_app.ref)
