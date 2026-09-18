import json
import os
import time

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-2")
MODEL_ID = os.environ["MODEL_ID"]
KNOWLEDGE_BUCKET = os.environ["KNOWLEDGE_BUCKET"]
KNOWLEDGE_KEY = os.environ.get("KNOWLEDGE_KEY", "projectknowledge.md")
ALLOWED_LOG_GROUPS = [g for g in os.environ.get("ALLOWED_LOG_GROUPS", "").split(",") if g]

bedrock_runtime = boto3.client("bedrock-runtime", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "content-type",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
}

_knowledge_cache = {"text": None, "loaded_at": 0.0}
_KNOWLEDGE_TTL_SECONDS = 60


def _load_knowledge() -> str:
    now = time.time()
    if _knowledge_cache["text"] is not None and (now - _knowledge_cache["loaded_at"]) < _KNOWLEDGE_TTL_SECONDS:
        return _knowledge_cache["text"]
    resp = s3.get_object(Bucket=KNOWLEDGE_BUCKET, Key=KNOWLEDGE_KEY)
    text = resp["Body"].read().decode("utf-8")
    _knowledge_cache["text"] = text
    _knowledge_cache["loaded_at"] = now
    return text


SEARCH_LOGS_TOOL = {
    "toolSpec": {
        "name": "search_logs",
        "description": (
            "Search real CloudWatch logs from the class demo applications, including the "
            "structured reasoning log (one JSON line per LLM call: user text, tools called, "
            "Guardrail status, response) from the Policy & Benefits Assistant's AgentCore "
            "Runtime, and this Insights app's own activity. Only a fixed allow-list of log "
            "groups can be searched -- nothing outside the class demo applications."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Substring to filter for, e.g. a username or 'guardrail_intervened'. Omit to return the most recent events.",
                    },
                    "minutes_ago": {
                        "type": "integer",
                        "description": "How far back to search, in minutes. Defaults to 1440 (24 hours).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max events to return, defaults to 15.",
                    },
                },
            }
        },
    }
}


def _system_prompt() -> str:
    knowledge = _load_knowledge()
    return (
        "You are the Class Insights Assistant for a live AWS class, \"Building Agentic AI "
        "with Amazon Bedrock AgentCore.\" You answer student questions about the design and "
        "deployment of the class's demo applications -- the Policy & Benefits Assistant "
        "(www.awsteach.com) and this Insights app itself -- grounded in the knowledge "
        "document below. Use the search_logs tool when a student asks about actual runtime "
        "activity, recent conversations, reasoning traces, or log contents -- don't guess at "
        "log data, fetch it for real. Be concise, and be honest about what's real "
        "infrastructure vs. simulated/synthetic data per the knowledge document.\n\n"
        "=== PROJECT KNOWLEDGE ===\n" + knowledge
    )


def search_logs(query: str = "", minutes_ago: int = 1440, limit: int = 15) -> dict:
    if not ALLOWED_LOG_GROUPS:
        return {"error": "No log groups configured."}
    start_time_ms = int((time.time() - max(1, minutes_ago) * 60) * 1000)
    filter_pattern = f'"{query}"' if query else None
    events = []
    for log_group in ALLOWED_LOG_GROUPS:
        try:
            kwargs = {
                "logGroupName": log_group,
                "startTime": start_time_ms,
                "limit": limit,
                "interleaved": True,
            }
            if filter_pattern:
                kwargs["filterPattern"] = filter_pattern
            resp = logs.filter_log_events(**kwargs)
            for e in resp.get("events", []):
                events.append({
                    "log_group": log_group,
                    "timestamp": e["timestamp"],
                    "message": e["message"][:2000],
                })
        except logs.exceptions.ResourceNotFoundException:
            continue
    events.sort(key=lambda e: e["timestamp"], reverse=True)
    return {"events": events[:limit], "log_groups_searched": ALLOWED_LOG_GROUPS}


def _converse(messages):
    return bedrock_runtime.converse(
        modelId=MODEL_ID,
        system=[{"text": _system_prompt()}],
        messages=messages,
        toolConfig={"tools": [SEARCH_LOGS_TOOL]},
        inferenceConfig={"maxTokens": 700, "temperature": 0.2},
    )


def _run_turn(messages: list) -> dict:
    tool_used = False
    for _ in range(4):
        resp = _converse(messages)
        output_message = resp["output"]["message"]
        messages.append(output_message)

        if resp["stopReason"] != "tool_use":
            text = "".join(b.get("text", "") for b in output_message.get("content", []) if "text" in b)
            return {"text": text, "tool_used": tool_used}

        tool_results = []
        for block in output_message.get("content", []):
            if "toolUse" not in block:
                continue
            tool_use = block["toolUse"]
            tool_use_id = tool_use["toolUseId"]
            if tool_use["name"] == "search_logs":
                tool_used = True
                output = search_logs(**tool_use.get("input", {}))
            else:
                output = {"error": f"Unknown tool '{tool_use['name']}'"}
            tool_results.append({
                "toolResult": {"toolUseId": tool_use_id, "content": [{"json": output}]}
            })
        messages.append({"role": "user", "content": tool_results})

    return {"text": "I wasn't able to finish that within the allowed number of steps.", "tool_used": tool_used}


def _response(status, body):
    return {"statusCode": status, "headers": CORS_HEADERS, "body": json.dumps(body)}


def handler(event, context):
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return _response(200, {})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Invalid JSON body"})

    message = body.get("message")
    if not message:
        return _response(400, {"error": "Missing 'message'"})

    history = body.get("history", [])
    messages = []
    for turn in history:
        role = turn.get("role")
        text = turn.get("text", "")
        if role in ("user", "assistant") and text:
            messages.append({"role": role, "content": [{"text": text}]})
    messages.append({"role": "user", "content": [{"text": message}]})

    result = _run_turn(messages)
    return _response(200, {"response": result["text"], "tool_used": result["tool_used"]})
