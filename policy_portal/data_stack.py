from aws_cdk import Stack, RemovalPolicy, CfnOutput
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct


class DataStack(Stack):
    """DynamoDB tables backing the mock secure API (leave balances, leave/
    benefits requests) and the S3 buckets holding the RAG corpus and the
    AgentCore Runtime's direct-code-deploy artifact.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.leave_balance_table = ddb.TableV2(
            self,
            "LeaveBalanceTable",
            partition_key=ddb.Attribute(name="username", type=ddb.AttributeType.STRING),
            billing=ddb.Billing.on_demand(),
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.requests_table = ddb.TableV2(
            self,
            "RequestsTable",
            partition_key=ddb.Attribute(name="request_id", type=ddb.AttributeType.STRING),
            billing=ddb.Billing.on_demand(),
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.requests_table.add_global_secondary_index(
            index_name="username-index",
            partition_key=ddb.Attribute(name="username", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="submitted_at", type=ddb.AttributeType.STRING),
        )

        # RAG corpus: synthetic policy documents, uploaded as a KB data source.
        self.corpus_bucket = s3.Bucket(
            self,
            "CorpusBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        s3deploy.BucketDeployment(
            self,
            "CorpusDeployment",
            sources=[s3deploy.Source.asset("knowledge/corpus")],
            destination_bucket=self.corpus_bucket,
            destination_key_prefix="corpus",
            retain_on_delete=False,
        )

        # AgentCore Runtime direct-code-deploy artifact. Versioned so a
        # redeploy of the same key can still be detected by VersionId.
        # NOTE: aws-cdk-lib's Bucket.bucket_name always returns a CDK Token
        # string, even when bucket_name= was given as a plain literal -- so
        # app.py can't resolve the real name from that property at synth
        # time. Keep the literal separately for boto3 pre-checks.
        self.agent_code_bucket_name = f"policyportal-agent-code-{self.account}-{self.region}"
        self.agent_code_bucket = s3.Bucket(
            self,
            "AgentCodeBucket",
            bucket_name=self.agent_code_bucket_name,
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        CfnOutput(self, "CorpusBucketName", value=self.corpus_bucket.bucket_name)
        CfnOutput(self, "AgentCodeBucketName", value=self.agent_code_bucket.bucket_name)
        CfnOutput(self, "LeaveBalanceTableName", value=self.leave_balance_table.table_name)
        CfnOutput(self, "RequestsTableName", value=self.requests_table.table_name)
