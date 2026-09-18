import asyncio
import datetime
import json
import os
import sys
import time
import traceback

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from mcp import ClientSession
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

MODEL_ID = os.environ["MODEL_ID"]
GATEWAY_URL = os.environ["GATEWAY_URL"]
KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]
GUARDRAIL_ID = os.environ["GUARDRAIL_ID"]
GUARDRAIL_VERSION = os.environ["GUARDRAIL_VERSION"]
MEMORY_ID = os.environ["MEMORY_ID"]
REGION = os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_REGION", "us-east-2")

bedrock_runtime = boto3.client("bedrock-runtime", region_name=REGION)
bedrock_agent_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)
agentcore_data = boto3.client("bedrock-agentcore", region_name=REGION)

MEMORY_SUMMARY_NAMESPACE = "/policyportal/{actor_id}/summary/"


def _load_recent_events(username: str, session_id: str, limit: int = 20) -> list:
    """Short-term memory: reconstruct this session's recent turns from
    AgentCore Memory instead of trusting a client-supplied history array
    (the old design -- the browser resent the entire conversation on every
    turn, which doesn't scale to weeks/months and loses everything on a
    page reload). Returns Converse-shaped {"role", "content"} messages.
    """
    try:
        resp = agentcore_data.list_events(
            memoryId=MEMORY_ID,
            actorId=username,
            sessionId=session_id,
            includePayloads=True,
            maxResults=limit,
        )
    except Exception as e:  # noqa: BLE001
        print(f"list_events failed: {e!r}", flush=True)
        return []
    # Sort defensively rather than assume a particular API ordering.
    events = sorted(resp.get("events", []), key=lambda e: e.get("eventTimestamp") or 0)
    messages = []
    for event in events:
        for item in event.get("payload", []):
            conv = item.get("conversational")
            if not conv:
                continue
            role = conv.get("role", "").lower()
            text = conv.get("content", {}).get("text", "")
            if role in ("user", "assistant") and text:
                messages.append({"role": role, "content": [{"text": text}]})
    return messages


def _load_long_term_context(username: str, query: str) -> str:
    """Long-term memory: semantic search over this user's extracted
    cross-session summary (populated asynchronously by the Memory
    resource's Summarization strategy -- not instant, and empty until the
    first session has had time to be summarized). Folded into the system
    prompt rather than replayed as raw transcript.
    """
    if not query:
        return ""
    namespace = MEMORY_SUMMARY_NAMESPACE.format(actor_id=username)
    try:
        resp = agentcore_data.retrieve_memory_records(
            memoryId=MEMORY_ID,
            namespace=namespace,
            searchCriteria={"searchQuery": query, "topK": 3},
        )
    except Exception as e:  # noqa: BLE001
        print(f"retrieve_memory_records failed: {e!r}", flush=True)
        return ""
    snippets = []
    for record in resp.get("memoryRecordSummaries", []):
        content = record.get("content") or {}
        text = content.get("text") if isinstance(content, dict) else None
        if text:
            snippets.append(text)
    return "\n".join(snippets)


def _write_turn_events(username: str, session_id: str, user_text: str, assistant_text: str) -> None:
    """Persist this turn (both sides) to AgentCore Memory as one atomic
    event. Only the human-readable exchange is stored -- intermediate
    tool-call JSON from the Converse loop is deliberately not persisted as
    conversation content, to keep memory readable and keep it "the
    conversation," not a technical trace (that's what the separate
    reasoning log is for).
    """
    if not user_text or not assistant_text:
        return
    try:
        agentcore_data.create_event(
            memoryId=MEMORY_ID,
            actorId=username,
            sessionId=session_id,
            eventTimestamp=datetime.datetime.now(datetime.timezone.utc),
            payload=[
                {"conversational": {"content": {"text": user_text}, "role": "USER"}},
                {"conversational": {"content": {"text": assistant_text}, "role": "ASSISTANT"}},
            ],
        )
    except Exception as e:  # noqa: BLE001
        print(f"create_event failed: {e!r}", flush=True)

SYSTEM_PROMPT = """You are the Policy & Benefits Assistant for a mixed government,
corporate, and education workforce portal. You help employees understand
organizational policy (leave, benefits, code of conduct, remote work, data
handling) and, when asked, check their own leave balance or submit a formal
leave/benefits request on their behalf.

You DO have memory: your recent turns in this session, and a summary of this
user's earlier sessions when relevant, are provided to you as conversation
history and context above -- don't tell the user you have no memory of past
interactions, since that's no longer true.

Rules:
- Always ground policy answers in the retrieve_policy_docs tool. If the corpus
  doesn't cover a question, say so plainly rather than guessing.
- Only call check_leave_balance or submit_request for the currently signed-in
  user (their username is provided to you in context) -- never for anyone else.
- Never ask the user to type a full Social Security Number, full financial
  account number, or similar highly sensitive identifier into the chat; if
  they need to provide one for a request, tell them a secure channel will
  follow up, consistent with the organization's Data Handling and Compliance
  Policy.
- Be concise and cite which policy section grounded your answer when you use
  retrieve_policy_docs.
"""

RETRIEVE_TOOL = {
    "toolSpec": {
        "name": "retrieve_policy_docs",
        "description": "Search the organization's policy/benefits handbook corpus for passages relevant to a question.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."},
                },
                "required": ["query"],
            }
        },
    }
}


def retrieve_policy_docs(query: str) -> dict:
    resp = bedrock_agent_runtime.retrieve(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": 5}},
    )
    results = []
    for r in resp.get("retrievalResults", []):
        results.append({
            "text": r.get("content", {}).get("text", ""),
            "score": r.get("score"),
            "source": r.get("location", {}).get("s3Location", {}).get("uri", ""),
        })
    return {"results": results}


def _mcp_tool_to_bedrock_spec(tool) -> dict:
    return {
        "toolSpec": {
            "name": tool.name,
            "description": tool.description or tool.name,
            "inputSchema": {"json": tool.inputSchema},
        }
    }


async def _call_gateway_tool(session: ClientSession, name: str, arguments: dict) -> dict:
    result = await session.call_tool(name, arguments)
    parts = []
    for c in result.content:
        text = getattr(c, "text", None)
        if text:
            parts.append(text)
    joined = "\n".join(parts) if parts else "{}"
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return {"raw": joined}


def _log_reasoning(*, session_id: str, username: str, messages: list, resp: dict) -> None:
    """One structured JSON line per Converse call -- the reasoning-audit trail
    the Class Insights companion app's search_logs tool reads. Must be
    print(..., flush=True): stdout inside the Runtime container is fully
    block-buffered, not line-buffered (see the tool-call error log below for
    the same lesson learned live).
    """
    output_message = resp.get("output", {}).get("message", {})
    tool_calls = [
        block["toolUse"]["name"]
        for block in output_message.get("content", [])
        if "toolUse" in block
    ]
    last_user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            texts = [b.get("text", "") for b in m.get("content", []) if "text" in b]
            if texts:
                last_user_text = texts[0][:500]
                break
    output_text = "".join(
        b.get("text", "") for b in output_message.get("content", []) if "text" in b
    )[:500]
    record = {
        "type": "reasoning_log",
        "ts": time.time(),
        "session_id": session_id,
        "username": username,
        "model_id": MODEL_ID,
        "last_user_text": last_user_text,
        "tool_calls": tool_calls,
        "stop_reason": resp.get("stopReason"),
        "guardrail_intervened": resp.get("stopReason") == "guardrail_intervened",
        "output_text": output_text,
    }
    print(json.dumps(record), flush=True)


def _converse(messages, tool_specs, long_term_context=""):
    system_blocks = [{"text": SYSTEM_PROMPT}]
    if long_term_context:
        system_blocks.append({
            "text": (
                "Relevant context extracted from this user's earlier sessions "
                f"(a summary, not verbatim transcript):\n{long_term_context}"
            )
        })
    return bedrock_runtime.converse(
        modelId=MODEL_ID,
        system=system_blocks,
        messages=messages,
        toolConfig={"tools": [RETRIEVE_TOOL] + tool_specs},
        inferenceConfig={"maxTokens": 800, "temperature": 0.2},
        guardrailConfig={
            "guardrailIdentifier": GUARDRAIL_ID,
            "guardrailVersion": GUARDRAIL_VERSION,
            "trace": "enabled",
        },
    )


async def _run_turn(username: str, session_id: str, messages: list, long_term_context: str = "") -> dict:
    """Opens a fresh Gateway MCP session for this invocation rather than
    reusing one cached at cold start. AgentCore Runtime invokes the handler
    in a fresh AnyIO worker thread each time (no guaranteed event-loop
    affinity across invocations), and anyio's MemoryObjectSendStream /
    ReceiveStream objects are bound to the event loop that created them --
    a session opened once at import time breaks on the next invocation's
    thread. Listing tools costs one extra Gateway round trip per turn, which
    is an acceptable tradeoff for correctness.
    """
    guardrail_intervened = False

    async with aws_iam_streamablehttp_client(
        GATEWAY_URL, aws_service="bedrock-agentcore", aws_region=REGION
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            tool_specs = [_mcp_tool_to_bedrock_spec(t) for t in tools]
            tool_names = {t.name for t in tools}

            for _ in range(6):  # bounded tool-use loop
                resp = _converse(messages, tool_specs, long_term_context)
                _log_reasoning(session_id=session_id, username=username, messages=messages, resp=resp)

                # `trace.guardrail` is present on every response regardless of
                # whether anything happened (confirmed live: a totally benign
                # message still returns a full trace tree with
                # `actionReason: "No action."`) -- the only reliable signal
                # that the Guardrail actually did something is the API's own
                # `stopReason` value.
                stop_reason = resp["stopReason"]
                if stop_reason == "guardrail_intervened":
                    guardrail_intervened = True

                output_message = resp["output"]["message"]
                messages.append(output_message)

                if stop_reason != "tool_use":
                    text = "".join(
                        block.get("text", "") for block in output_message.get("content", []) if "text" in block
                    )
                    return {"text": text, "guardrail_intervened": guardrail_intervened}

                tool_results = []
                for block in output_message.get("content", []):
                    if "toolUse" not in block:
                        continue
                    tool_use = block["toolUse"]
                    tool_name = tool_use["name"]
                    tool_input = tool_use.get("input", {})
                    tool_use_id = tool_use["toolUseId"]

                    try:
                        if tool_name == "retrieve_policy_docs":
                            output = retrieve_policy_docs(**tool_input)
                        elif tool_name in tool_names:
                            call_input = dict(tool_input)
                            call_input.setdefault("username", username)
                            output = await _call_gateway_tool(session, tool_name, call_input)
                        else:
                            output = {"error": f"Unknown tool '{tool_name}'"}
                    except Exception as e:  # noqa: BLE001
                        # stdout is fully buffered (not line-buffered) inside
                        # the Runtime container, so a bare print() here can
                        # sit unflushed until the container is later reused
                        # or recycled -- confirmed live (the structured
                        # "Invocation completed" log line appeared
                        # immediately, this print did not, until flush=True
                        # was added).
                        print(f"Tool call failed: {tool_name} input={tool_input!r} error={e!r}", flush=True)
                        traceback.print_exc()
                        sys.stderr.flush()
                        output = {"error": str(e)}

                    tool_results.append({
                        "toolResult": {
                            "toolUseId": tool_use_id,
                            "content": [{"json": output}],
                        }
                    })

                messages.append({"role": "user", "content": tool_results})

    return {
        "text": "I wasn't able to finish that request within the allowed number of steps.",
        "guardrail_intervened": guardrail_intervened,
    }


app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload):
    prompt = payload.get("prompt", "")
    username = payload.get("username", "unknown")
    session_id = payload.get("session_id", "")

    # Conversation state now lives server-side in AgentCore Memory, keyed
    # on (username, session_id) -- not resent by the client. Short-term:
    # this session's recent turns. Long-term: a semantic summary of what
    # this user has discussed across ALL of their sessions.
    messages = _load_recent_events(username, session_id)
    long_term_context = _load_long_term_context(username, prompt)

    messages.append({"role": "user", "content": [{"text": f"[signed in as: {username}] {prompt}"}]})

    result = asyncio.run(_run_turn(username, session_id, messages, long_term_context))

    _write_turn_events(username, session_id, prompt, result["text"])

    return {
        "response": result["text"],
        "session_id": session_id,
        "guardrail_intervened": result["guardrail_intervened"],
    }


if __name__ == "__main__":
    app.run()
