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
from policy_portal.agent_runtime_stack import AgentRuntimeStack, AGENT_CODE_KEY, ENDPOINT_NAME, MODEL_ID as DEFAULT_MODEL_ID
from policy_portal.api_stack import ApiStack
from policy_portal.cert_stack import CertStack
from policy_portal.frontend_stack import FrontendStack
from policy_portal.insights_site_stack import InsightsSiteStack
from policy_portal.insights_api_stack import InsightsApiStack
from policy_portal.model_selector_stack import ModelSelectorStack
from policy_portal.memory_stack import MemoryStack

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

memory = MemoryStack(app, "PolicyPortal-Memory", env=main_env)


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
        memory_id=memory.memory.memory_id,
        memory_arn=memory.memory.memory_arn,
    )
    agent_runtime.add_dependency(network)
    agent_runtime.add_dependency(data)
    agent_runtime.add_dependency(gateway)
    agent_runtime.add_dependency(kb)
    agent_runtime.add_dependency(guardrail)
    agent_runtime.add_dependency(memory)

    api = ApiStack(
        app, "PolicyPortal-Api", env=main_env,
        vpc=network.vpc,
        lambda_sg=network.lambda_sg,
        agent_runtime_arn=agent_runtime.runtime.attr_agent_runtime_arn,
        agent_runtime_endpoint_name=agent_runtime.endpoint.name,
        memory_id=memory.memory.memory_id,
        memory_arn=memory.memory.memory_arn,
    )
    api.add_dependency(agent_runtime)
    api.add_dependency(memory)

    insights_site = InsightsSiteStack(app, "PolicyPortal-InsightsSite", env=main_env)

    insights_api = InsightsApiStack(
        app, "PolicyPortal-InsightsApi", env=main_env,
        vpc=network.vpc,
        lambda_sg=network.lambda_sg,
        knowledge_bucket=insights_site.knowledge_bucket,
        allowed_log_groups=[
            # The agent's structured reasoning log -- confirmed live that the
            # real content lands under the "-prod" endpoint-qualified suffix,
            # not "-DEFAULT" (see agent_runtime_stack.py / DESIGN.md).
            # NOTE: attr_agent_runtime_id already returns the full
            # "<name>-<suffix>" form (e.g. "policy_portal_agent-5UW6I96cI3"),
            # not just the bare suffix -- prepending RUNTIME_NAME again here
            # produced a doubled, non-existent log group name
            # ("policy_portal_agent-policy_portal_agent-...") that silently
            # matched zero events instead of erroring, confirmed live via
            # `aws lambda get-function-configuration` showing the bad value.
            f"/aws/bedrock-agentcore/runtimes/{agent_runtime.runtime.attr_agent_runtime_id}-prod",
            f"/aws/lambda/{api.proxy_fn.function_name}",
            f"/aws/lambda/{gateway.tools_fn.function_name}",
        ],
    )
    insights_api.add_dependency(insights_site)
    insights_api.add_dependency(agent_runtime)
    insights_api.add_dependency(api)
    insights_api.add_dependency(gateway)

    model_selector = ModelSelectorStack(
        app, "PolicyPortal-ModelSelector", env=main_env,
        runtime_id=agent_runtime.runtime.attr_agent_runtime_id,
        runtime_arn=agent_runtime.runtime.attr_agent_runtime_arn,
        runtime_role_arn=agent_runtime.runtime_role.role_arn,
        agent_code_bucket=data.agent_code_bucket,
        endpoint_name=ENDPOINT_NAME,
        default_model_id=DEFAULT_MODEL_ID,
    )
    model_selector.add_dependency(agent_runtime)
else:
    print(
        "\n*** PolicyPortal-AgentRuntime / PolicyPortal-Api skipped: "
        "agent code not yet uploaded to the AgentCodeBucket. Deploy the other "
        "stacks first, run scripts/package_agent.sh, then run cdk deploy --all again. ***\n"
    )

app.synth()
