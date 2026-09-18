from aws_cdk import Stack
from aws_cdk import aws_ec2 as ec2
from constructs import Construct


class NetworkStack(Stack):
    """VPC-isolated network: no NAT, no internet gateway. All AWS-service
    access goes over PrivateLink interface endpoints (or the S3 gateway
    endpoint), consistent with the compliance-focused audience for this app.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr("10.90.0.0/16"),
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="isolated",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                ),
            ],
        )

        self.endpoint_sg = ec2.SecurityGroup(
            self,
            "EndpointSecurityGroup",
            vpc=self.vpc,
            description="Allow HTTPS from within the VPC to interface endpoints",
            allow_all_outbound=False,
        )
        self.endpoint_sg.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.tcp(443),
            "HTTPS from VPC CIDR",
        )

        interface_services = {
            "BedrockRuntime": ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
            "BedrockAgentRuntime": ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENT_RUNTIME,
            "BedrockAgentCore": ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENTCORE,
            "BedrockAgentCoreGateway": ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENTCORE_GATEWAY,
            # Mandatory: VPC Lambdas hang with no output if the logs endpoint is missing.
            "Logs": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            "Sts": ec2.InterfaceVpcEndpointAwsService.STS,
            "CognitoIdp": ec2.InterfaceVpcEndpointAwsService.COGNITO_IDP,
        }

        self.interface_endpoints = {}
        for name, service in interface_services.items():
            if service is None:
                continue
            self.interface_endpoints[name] = self.vpc.add_interface_endpoint(
                f"{name}Endpoint",
                service=service,
                subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
                security_groups=[self.endpoint_sg],
                private_dns_enabled=True,
            )

        self.vpc.add_gateway_endpoint(
            "S3GatewayEndpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )
        self.vpc.add_gateway_endpoint(
            "DynamoDbGatewayEndpoint",
            service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
        )

        self.lambda_sg = ec2.SecurityGroup(
            self,
            "LambdaSecurityGroup",
            vpc=self.vpc,
            description="Security group for VPC-isolated Lambdas in this stack",
            allow_all_outbound=True,
        )
