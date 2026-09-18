import json
import os
import boto3
from botocore.exceptions import ClientError

cognito = boto3.client("cognito-idp")
agentcore = boto3.client("bedrock-agentcore")

RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]
ENDPOINT_NAME = os.environ["AGENT_RUNTIME_ENDPOINT_NAME"]
MEMORY_ID = os.environ["MEMORY_ID"]

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "authorization,content-type",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
}


def _load_history(username: str, session_id: str) -> list:
    """Read-only replay of a session's short-term memory, for the frontend
    to redraw a conversation after a page reload without the browser ever
    having stored it itself. Conversation state now lives server-side in
    AgentCore Memory (see agents/policy_agent/agent.py) -- this function
    does not call the model, it just reads what's already there.
    """
    try:
        resp = agentcore.list_events(
            memoryId=MEMORY_ID,
            actorId=username,
            sessionId=session_id,
            includePayloads=True,
            maxResults=50,
        )
    except ClientError:
        return []
    events = sorted(resp.get("events", []), key=lambda e: e.get("eventTimestamp") or 0)
    turns = []
    for event in events:
        for item in event.get("payload", []):
            conv = item.get("conversational")
            if not conv:
                continue
            role = conv.get("role", "").lower()
            text = conv.get("content", {}).get("text", "")
            if role in ("user", "assistant") and text:
                turns.append({"role": role, "text": text})
    return turns


def _response(status, body):
    return {"statusCode": status, "headers": CORS_HEADERS, "body": json.dumps(body)}


def _authenticate(event):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    token = auth[len("Bearer "):] if auth.lower().startswith("bearer ") else auth
    if not token:
        return None
    try:
        resp = cognito.get_user(AccessToken=token)
        return resp["Username"]
    except ClientError:
        return None


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method")
    if method == "OPTIONS":
        return _response(200, {})

    username = _authenticate(event)
    if not username:
        return _response(401, {"error": "Unauthorized"})

    if method == "GET":
        qs = event.get("queryStringParameters") or {}
        session_id = qs.get("session_id")
        if not session_id:
            return _response(400, {"error": "Missing 'session_id'"})
        return _response(200, {"history": _load_history(username, session_id)})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Invalid JSON body"})

    message = body.get("message")
    session_id = body.get("session_id") or f"session-{username}-{context.aws_request_id}"
    # AgentCore requires a runtimeSessionId of at least 33 characters.
    if len(session_id) < 33:
        session_id = session_id.ljust(33, "0")
    if not message:
        return _response(400, {"error": "Missing 'message'"})

    # Conversation history is no longer client-supplied -- the agent
    # itself reconstructs recent turns from AgentCore Memory using
    # (username, session_id). This proxy's only job is still just to
    # authenticate and invoke.
    payload = json.dumps({
        "prompt": message,
        "username": username,
        "session_id": session_id,
    }).encode("utf-8")

    try:
        resp = agentcore.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            qualifier=ENDPOINT_NAME,
            runtimeSessionId=session_id,
            payload=payload,
        )
        raw = resp["response"].read()
        result = json.loads(raw)
    except ClientError as e:
        return _response(502, {"error": f"Agent invocation failed: {e}"})

    return _response(200, result)
