from aws_cdk import Stack
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_route53 as route53
from constructs import Construct

DOMAIN_NAME = "www.awsteach.com"
ZONE_NAME = "awsteach.com"
HOSTED_ZONE_ID = "Z02882605B7GUS0L3VHX"


class CertStack(Stack):
    """ACM certificate for the CloudFront distribution. Must live in
    us-east-1 regardless of which region the rest of the app runs in --
    a CloudFront hard requirement.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.hosted_zone = route53.HostedZone.from_hosted_zone_attributes(
            self,
            "HostedZone",
            hosted_zone_id=HOSTED_ZONE_ID,
            zone_name=ZONE_NAME,
        )

        self.certificate = acm.Certificate(
            self,
            "Certificate",
            domain_name=DOMAIN_NAME,
            validation=acm.CertificateValidation.from_dns(self.hosted_zone),
        )
