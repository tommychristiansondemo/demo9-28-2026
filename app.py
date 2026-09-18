#!/usr/bin/env python3
import boto3
from botocore.exceptions import ClientError
import aws_cdk as cdk

from policy_portal.network_stack import NetworkStack
from policy_portal.auth_stack import AuthStack
from policy_portal.data_stack import DataStack
from policy_portal.guardrail_stack import GuardrailStack
from policy_portal.knowledge_base_stack import KnowledgeBaseStack
from policy_portal.gateway_stack import GatewayStack
from policy_portal.agent_runtime_stack import AgentRuntimeStack, AGENT_CODE_KEY
from policy_portal.api_stack import ApiStack
from policy_portal.cert_stack import CertStack
from policy_portal.frontend_stack import FrontendStack

ACCOUNT = "154833006816"
REGION = "us-east-2"
CERT_REGION = "us-east-1"

app = cdk.App()

main_env = cdk.Environment(account=ACCOUNT, region=REGION)
cert_env = cdk.Environment(account=ACCOUNT, region=CERT_REGION)

network = NetworkStack(app, "PolicyPortal-Network", env=main_env)
auth = AuthStack(app, "PolicyPortal-Auth", env=main_env)
data = DataStack(app, "PolicyPortal-Data", env=main_env)
guardrail = GuardrailStack(app, "PolicyPortal-Guardrail", env=main_env)

kb = KnowledgeBaseStack(
    app, "PolicyPortal-KnowledgeBase", env=main_env,
    corpus_bucket=data.corpus_bucket,
)
kb.add_dependency(data)

gateway = GatewayStack(
    app, "PolicyPortal-Gateway", env=main_env,
    leave_balance_table=data.leave_balance_table,
    requests_table=data.requests_table,
)
gateway.add_dependency(data)

cert_stack = CertStack(app, "PolicyPortal-Cert", env=cert_env, cross_region_references=True)

frontend = FrontendStack(
    app, "PolicyPortal-Frontend", env=main_env, cross_region_references=True,
    certificate=cert_stack.certificate,
)
frontend.add_dependency(cert_stack)


def _agent_code_exists() -> bool:
    """Two-phase deploy, same pattern as the ECR-image ordering in prior
    classes: AgentRuntimeStack (and ApiStack, which depends on it) can only
    synth once scripts/package_agent.sh has uploaded a real code zip to
    PolicyPortal-Data's agent_code_bucket. Until then, this app simply omits
    those two stacks so `cdk deploy --all` deploys everything else first.
    """
    try:
        s3 = boto3.client("s3", region_name=REGION)
        s3.head_object(Bucket=data.agent_code_bucket_name, Key=AGENT_CODE_KEY)
        return True
    except ClientError:
        return False
    except Exception:
        return False


if _agent_code_exists():
    agent_runtime = AgentRuntimeStack(
        app, "PolicyPortal-AgentRuntime", env=main_env,
        vpc=network.vpc,
        lambda_sg=network.lambda_sg,
        agent_code_bucket=data.agent_code_bucket,
        agent_code_bucket_name=data.agent_code_bucket_name,
        gateway_url=gateway.gateway.attr_gateway_url,
        gateway_arn=gateway.gateway.attr_gateway_arn,
        knowledge_base_id=kb.knowledge_base.attr_knowledge_base_id,
        guardrail_id=guardrail.guardrail.attr_guardrail_id,
        guardrail_version="DRAFT",
    )
    agent_runtime.add_dependency(network)
    agent_runtime.add_dependency(data)
    agent_runtime.add_dependency(gateway)
    agent_runtime.add_dependency(kb)
    agent_runtime.add_dependency(guardrail)

    api = ApiStack(
        app, "PolicyPortal-Api", env=main_env,
        vpc=network.vpc,
        lambda_sg=network.lambda_sg,
        agent_runtime_arn=agent_runtime.runtime.attr_agent_runtime_arn,
        agent_runtime_endpoint_name=agent_runtime.endpoint.name,
    )
    api.add_dependency(agent_runtime)
else:
    print(
        "\n*** PolicyPortal-AgentRuntime / PolicyPortal-Api skipped: "
        "agent code not yet uploaded to the AgentCodeBucket. Deploy the other "
        "stacks first, run scripts/package_agent.sh, then run cdk deploy --all again. ***\n"
    )

app.synth()
