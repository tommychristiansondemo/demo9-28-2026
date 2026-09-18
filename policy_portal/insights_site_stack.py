from aws_cdk import Stack, CfnOutput, RemovalPolicy
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from constructs import Construct


class InsightsSiteStack(Stack):
    """Static hosting for the Class Insights companion app: a public site
    bucket behind CloudFront (default *.cloudfront.net domain -- deliberately
    no custom subdomain/ACM cert, to skip an unnecessary DNS-validation wait
    for an internal teaching tool) and a *private* knowledge bucket holding
    projectknowledge.md, readable only by the insights-chat Lambda's role.

    The BucketDeployment for projectknowledge.md re-syncs on every deploy of
    this stack, which is the mechanism for "keep it updated as new
    functionality is deployed": edit knowledge/insights/projectknowledge.md,
    redeploy this stack, done.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.knowledge_bucket = s3.Bucket(
            self,
            "KnowledgeBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        s3deploy.BucketDeployment(
            self,
            "KnowledgeDeployment",
            sources=[s3deploy.Source.asset("knowledge/insights")],
            destination_bucket=self.knowledge_bucket,
            retain_on_delete=False,
        )

        self.site_bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        s3deploy.BucketDeployment(
            self,
            "SiteDeployment",
            sources=[s3deploy.Source.asset("insights-frontend/public")],
            destination_bucket=self.site_bucket,
            retain_on_delete=False,
        )

        self.distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_root_object="index.html",
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
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
            ],
        )

        CfnOutput(self, "KnowledgeBucketName", value=self.knowledge_bucket.bucket_name)
        CfnOutput(self, "SiteBucketName", value=self.site_bucket.bucket_name)
        CfnOutput(self, "DistributionId", value=self.distribution.distribution_id)
        CfnOutput(self, "InsightsSiteUrl", value=f"https://{self.distribution.distribution_domain_name}")
