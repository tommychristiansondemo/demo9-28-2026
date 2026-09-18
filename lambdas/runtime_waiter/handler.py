import time
import boto3

control = boto3.client("bedrock-agentcore-control")


def handler(event, context):
    """Custom resource: AWS::BedrockAgentCore::Runtime reports CREATE_COMPLETE
    in CloudFormation while the agent version is still CREATING/UPDATING.
    Creating a RuntimeEndpoint right after fails with
    'Agent version N must be in READY status'. Poll here until it settles.
    """
    request_type = event["RequestType"]
    props = event["ResourceProperties"]
    runtime_id = props["AgentRuntimeId"]

    if request_type == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId", f"{runtime_id}-waiter")}

    deadline = time.time() + 480
    status = "UNKNOWN"
    while time.time() < deadline:
        resp = control.get_agent_runtime(agentRuntimeId=runtime_id)
        status = resp["status"]
        if status == "READY":
            return {"PhysicalResourceId": f"{runtime_id}-waiter", "Data": {"Status": status}}
        if status in ("CREATE_FAILED", "UPDATE_FAILED"):
            raise RuntimeError(f"Agent runtime {runtime_id} failed: {status} - {resp.get('failureReason', '')}")
        time.sleep(10)

    raise RuntimeError(f"Timed out waiting for agent runtime {runtime_id} to reach READY, last status={status}")
