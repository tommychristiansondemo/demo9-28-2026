from aws_cdk import Stack, Duration, RemovalPolicy, CfnOutput
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_lambda as lambda_
from constructs import Construct


class AuthStack(Stack):
    """Shared Cognito user pool: self-signup, username-only, 6-char minimum
    password, no complexity rules, PreSignUp Lambda auto-confirms. Same
    zero-friction pattern used in the prior three class demos.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        presignup_fn = lambda_.Function(
            self,
            "PreSignUpFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/presignup"),
            timeout=Duration.seconds(10),
        )

        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name="PolicyPortalUsers",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(username=True, email=False),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=False, mutable=True),
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=6,
                require_lowercase=False,
                require_uppercase=False,
                require_digits=False,
                require_symbols=False,
            ),
            account_recovery=cognito.AccountRecovery.NONE,
            lambda_triggers=cognito.UserPoolTriggers(pre_sign_up=presignup_fn),
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.user_pool_client = self.user_pool.add_client(
            "PortalClient",
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            generate_secret=False,
            access_token_validity=Duration.hours(4),
            id_token_validity=Duration.hours(4),
            refresh_token_validity=Duration.days(30),
        )

        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=self.user_pool_client.user_pool_client_id)
