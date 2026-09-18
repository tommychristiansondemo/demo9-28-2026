from aws_cdk import Stack, CfnOutput, Duration
from aws_cdk import aws_bedrockagentcore as bac
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_dynamodb as ddb
from constructs import Construct

GATEWAY_NAME = "policy-portal-gateway"
TARGET_NAME = "policy-portal-tools"


def _schema(type_, description=None, properties=None, required=None, items=None):
    kwargs = {"type": type_}
    if description is not None:
        kwargs["description"] = description
    if properties is not None:
        kwargs["properties"] = properties
    if required is not None:
        kwargs["required"] = required
    if items is not None:
        kwargs["items"] = items
    return bac.CfnGatewayTarget.SchemaDefinitionProperty(**kwargs)


class GatewayStack(Stack):
    """AgentCore Gateway exposing the mock secure leave/benefits API as MCP
    tools -- this is the 'Action Groups' equivalent for this app, since
    classic Bedrock Agents new-agent-creation is blocked (Maintenance Mode)
    in this account.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        leave_balance_table: ddb.TableV2,
        requests_table: ddb.TableV2,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.tools_fn = lambda_.Function(
            self,
            "ToolsFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/tools"),
            timeout=Duration.seconds(15),
            environment={
                "LEAVE_BALANCE_TABLE": leave_balance_table.table_name,
                "REQUESTS_TABLE": requests_table.table_name,
            },
        )
        leave_balance_table.grant_read_data(self.tools_fn)
        requests_table.grant_read_write_data(self.tools_fn)

        gateway_role = iam.Role(
            self,
            "GatewayRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )
        self.tools_fn.grant_invoke(gateway_role)

        self.gateway = bac.CfnGateway(
            self,
            "Gateway",
            name=GATEWAY_NAME,
            role_arn=gateway_role.role_arn,
            authorizer_type="AWS_IAM",
            protocol_type="MCP",
            protocol_configuration=bac.CfnGateway.GatewayProtocolConfigurationProperty(
                mcp=bac.CfnGateway.MCPGatewayConfigurationProperty(
                    supported_versions=["2025-03-26"],
                ),
            ),
        )

        check_balance_tool = bac.CfnGatewayTarget.ToolDefinitionProperty(
            name="check_leave_balance",
            description="Look up the signed-in user's current PTO and sick leave balances.",
            input_schema=_schema(
                "object",
                properties={
                    "username": _schema("string", description="The Cognito username of the signed-in employee."),
                },
                required=["username"],
            ),
        )
        submit_request_tool = bac.CfnGatewayTarget.ToolDefinitionProperty(
            name="submit_request",
            description="Submit a formal leave or benefits request on behalf of the signed-in user.",
            input_schema=_schema(
                "object",
                properties={
                    "username": _schema("string", description="The Cognito username of the signed-in employee."),
                    "request_type": _schema("string", description="Type of request, e.g. PTO, SICK, LEAVE_OF_ABSENCE, BENEFITS_ENROLLMENT_CHANGE."),
                    "start_date": _schema("string", description="Start date, YYYY-MM-DD."),
                    "end_date": _schema("string", description="End date, YYYY-MM-DD."),
                    "reason": _schema("string", description="Brief reason for the request."),
                },
                required=["username", "request_type"],
            ),
        )

        self.gateway_target = bac.CfnGatewayTarget(
            self,
            "GatewayTarget",
            gateway_identifier=self.gateway.attr_gateway_identifier,
            name=TARGET_NAME,
            target_configuration=bac.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=bac.CfnGatewayTarget.McpTargetConfigurationProperty(
                    lambda_=bac.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                        lambda_arn=self.tools_fn.function_arn,
                        tool_schema=bac.CfnGatewayTarget.ToolSchemaProperty(
                            inline_payload=[check_balance_tool, submit_request_tool],
                        ),
                    ),
                ),
            ),
            credential_provider_configurations=[
                bac.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE",
                ),
            ],
        )

        CfnOutput(self, "GatewayId", value=self.gateway.attr_gateway_identifier)
        CfnOutput(self, "GatewayUrl", value=self.gateway.attr_gateway_url)
