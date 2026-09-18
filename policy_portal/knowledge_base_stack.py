from aws_cdk import Stack, CfnOutput, Duration, CustomResource
from aws_cdk import aws_s3vectors as s3v
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_lambda as lambda_
from aws_cdk import custom_resources as cr
from constructs import Construct

EMBEDDING_DIMENSION = 1024
VECTOR_BUCKET_NAME = "policy-portal-vectors"
VECTOR_INDEX_NAME = "policy-portal-index"


class KnowledgeBaseStack(Stack):
    """Bedrock Knowledge Base for RAG over the synthetic policy corpus, backed
    by S3 Vectors (cheaper than OpenSearch Serverless for a one-day class --
    no idle OCU floor).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        corpus_bucket: s3.Bucket,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        vector_bucket = s3v.CfnVectorBucket(
            self,
            "VectorBucket",
            vector_bucket_name=VECTOR_BUCKET_NAME,
            encryption_configuration=s3v.CfnVectorBucket.EncryptionConfigurationProperty(
                sse_type="AES256",
            ),
        )

        vector_index = s3v.CfnIndex(
            self,
            "VectorIndex",
            index_name=VECTOR_INDEX_NAME,
            vector_bucket_name=VECTOR_BUCKET_NAME,
            data_type="float32",
            dimension=EMBEDDING_DIMENSION,
            distance_metric="cosine",
            metadata_configuration=s3v.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"],
            ),
        )
        vector_index.add_dependency(vector_bucket)

        kb_role = iam.Role(
            self,
            "KnowledgeBaseRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/amazon.titan-embed-text-v2:0",
                ],
            )
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3vectors:*"],
                resources=[
                    f"arn:aws:s3vectors:{self.region}:{self.account}:bucket/{VECTOR_BUCKET_NAME}",
                    f"arn:aws:s3vectors:{self.region}:{self.account}:bucket/{VECTOR_BUCKET_NAME}/index/{VECTOR_INDEX_NAME}",
                ],
            )
        )
        corpus_bucket.grant_read(kb_role)

        self.knowledge_base = bedrock.CfnKnowledgeBase(
            self,
            "KnowledgeBase",
            name="policy-portal-kb",
            description="Synthetic org policy/benefits handbook corpus for RAG.",
            role_arn=kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=f"arn:aws:bedrock:{self.region}::foundation-model/amazon.titan-embed-text-v2:0",
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="S3_VECTORS",
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                    vector_bucket_arn=vector_bucket.attr_vector_bucket_arn,
                    index_arn=vector_index.attr_index_arn,
                ),
            ),
        )
        # KB creation races its own role's policy attachment in CloudFormation.
        self.knowledge_base.node.add_dependency(kb_role)
        default_policy = kb_role.node.try_find_child("DefaultPolicy")
        if default_policy is not None:
            self.knowledge_base.node.add_dependency(default_policy)
        self.knowledge_base.add_dependency(vector_index)

        self.data_source = bedrock.CfnDataSource(
            self,
            "CorpusDataSource",
            knowledge_base_id=self.knowledge_base.attr_knowledge_base_id,
            name="synthetic-policy-corpus",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=corpus_bucket.bucket_arn,
                    inclusion_prefixes=["corpus/"],
                ),
            ),
            data_deletion_policy="DELETE",
        )

        # Custom resource to kick off (and wait for) an ingestion job whenever
        # this stack deploys, so the corpus is searchable right after `cdk deploy`.
        ingest_fn = lambda_.Function(
            self,
            "IngestTriggerFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/kb_ingest"),
            timeout=Duration.minutes(10),
        )
        ingest_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:StartIngestionJob",
                    "bedrock:GetIngestionJob",
                    "bedrock:ListIngestionJobs",
                ],
                resources=["*"],
            )
        )

        provider = cr.Provider(self, "IngestProvider", on_event_handler=ingest_fn)
        ingest_cr = CustomResource(
            self,
            "IngestTrigger",
            service_token=provider.service_token,
            properties={
                "KnowledgeBaseId": self.knowledge_base.attr_knowledge_base_id,
                "DataSourceId": self.data_source.attr_data_source_id,
                # Bump this to force a re-ingest on redeploy even with no other prop changes.
                "TriggerVersion": "1",
            },
        )
        ingest_cr.node.add_dependency(self.data_source)

        CfnOutput(self, "KnowledgeBaseId", value=self.knowledge_base.attr_knowledge_base_id)
        CfnOutput(self, "DataSourceId", value=self.data_source.attr_data_source_id)
