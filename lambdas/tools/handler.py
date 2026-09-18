import os
import uuid
import datetime
import boto3

dynamodb = boto3.resource("dynamodb")
leave_table = dynamodb.Table(os.environ["LEAVE_BALANCE_TABLE"])
requests_table = dynamodb.Table(os.environ["REQUESTS_TABLE"])


def _tool_name(event, context):
    """AgentCore Gateway invokes a Lambda target with the tool name in the
    Lambda **client context** (context.client_context.custom), not in the
    event body -- confirmed live: event.get("bedrockAgentCoreToolName")
    and friends are always absent from the event itself, which is why every
    tool call returned "Unknown tool ''" until this was fixed. The tool
    name arrives delimited by '___' from the target name, e.g.
    'policy-portal-tools___check_leave_balance'.
    """
    ctx = ""
    client_context = getattr(context, "client_context", None)
    if client_context is not None and getattr(client_context, "custom", None):
        ctx = client_context.custom.get("bedrockAgentCoreToolName", "")
    if not ctx:
        ctx = event.get("bedrockAgentCoreToolName") or event.get("toolName") or ""
    if "___" in ctx:
        return ctx.split("___")[-1]
    return ctx


def check_leave_balance(payload):
    username = payload["username"]
    item = leave_table.get_item(Key={"username": username}).get("Item")
    if not item:
        return {"error": f"No leave balance record found for user '{username}'."}
    return {
        "username": username,
        "pto_days_remaining": float(item.get("pto_days_remaining", 0)),
        "sick_days_remaining": float(item.get("sick_days_remaining", 0)),
        "as_of": item.get("as_of"),
    }


def submit_request(payload):
    request_id = str(uuid.uuid4())
    now = datetime.datetime.utcnow().isoformat() + "Z"
    item = {
        "request_id": request_id,
        "username": payload["username"],
        "request_type": payload["request_type"],
        "start_date": payload.get("start_date", ""),
        "end_date": payload.get("end_date", ""),
        "reason": payload.get("reason", ""),
        "status": "PENDING",
        "submitted_at": now,
    }
    requests_table.put_item(Item=item)
    return {
        "request_id": request_id,
        "status": "PENDING",
        "submitted_at": now,
        "message": "Request submitted successfully and is pending supervisor approval.",
    }


TOOLS = {
    "check_leave_balance": check_leave_balance,
    "submit_request": submit_request,
}


def handler(event, context):
    tool_name = _tool_name(event, context)
    fn = TOOLS.get(tool_name)
    if fn is None:
        return {"error": f"Unknown tool '{tool_name}'. Available tools: {list(TOOLS.keys())}"}
    try:
        return fn(event)
    except KeyError as e:
        return {"error": f"Missing required field: {e}"}
