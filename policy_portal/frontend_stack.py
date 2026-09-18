from aws_cdk import Stack, CfnOutput, RemovalPolicy, Duration
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as targets
from constructs import Construct

DOMAIN_NAME = "www.awsteach.com"
ZONE_NAME = "awsteach.com"
HOSTED_ZONE_ID = "Z02882605B7GUS0L3VHX"


class FrontendStack(Stack):
    """Static SPA frontend: S3 (private, OAC) + CloudFront + ACM + Route 53
    alias at www.awsteach.com. Reads a runtime /config.json (written by
    scripts/deploy_frontend.sh from stack outputs) so a backend redeploy
    doesn't require a frontend rebuild.
    """

    def __init__(self, scope: Construct, construct_id: str, certificate: acm.Certificate, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.site_bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        hosted_zone = route53.HostedZone.from_hosted_zone_attributes(
            self,
            "HostedZone",
            hosted_zone_id=HOSTED_ZONE_ID,
            zone_name=ZONE_NAME,
        )

        self.distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_root_object="index.html",
            domain_names=[DOMAIN_NAME],
            certificate=certificate,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
            ],
        )

        route53.ARecord(
            self,
            "AliasRecord",
            zone=hosted_zone,
            record_name=DOMAIN_NAME,
            target=route53.RecordTarget.from_alias(targets.CloudFrontTarget(self.distribution)),
        )

        CfnOutput(self, "SiteBucketName", value=self.site_bucket.bucket_name)
        CfnOutput(self, "DistributionId", value=self.distribution.distribution_id)
        CfnOutput(self, "SiteUrl", value=f"https://{DOMAIN_NAME}")
