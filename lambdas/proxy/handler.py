import json
import os
import boto3
from botocore.exceptions import ClientError

cognito = boto3.client("cognito-idp")
agentcore = boto3.client("bedrock-agentcore")

RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]
ENDPOINT_NAME = os.environ["AGENT_RUNTIME_ENDPOINT_NAME"]

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "authorization,content-type",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
}


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
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return _response(200, {})

    username = _authenticate(event)
    if not username:
        return _response(401, {"error": "Unauthorized"})

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

    payload = json.dumps({
        "prompt": message,
        "username": username,
        "session_id": session_id,
        "history": body.get("history", []),
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
