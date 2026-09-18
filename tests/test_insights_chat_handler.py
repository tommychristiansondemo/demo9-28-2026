import json
import os
from unittest.mock import MagicMock

from tests.conftest import load_lambda_module

os.environ["MODEL_ID"] = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
os.environ["KNOWLEDGE_BUCKET"] = "test-insights-knowledge"
os.environ["KNOWLEDGE_KEY"] = "projectknowledge.md"
os.environ["ALLOWED_LOG_GROUPS"] = "/aws/bedrock-agentcore/runtimes/policy_portal_agent-abc-prod,/aws/lambda/insights-chat"

insights = load_lambda_module(
    "insights_chat_handler", os.path.join(os.path.dirname(__file__), "..", "lambdas", "insights_chat", "handler.py")
)


class FakeContext:
    aws_request_id = "22222222-2222-2222-2222-222222222222"


def _fake_s3_get_object(**kwargs):
    return {"Body": MagicMock(read=lambda: b"# Test knowledge\nThis is a test doc.")}


def test_missing_message_returns_400():
    event = {"body": json.dumps({})}
    result = insights.handler(event, FakeContext())
    assert result["statusCode"] == 400


def test_options_request_returns_200():
    event = {"requestContext": {"http": {"method": "OPTIONS"}}}
    result = insights.handler(event, FakeContext())
    assert result["statusCode"] == 200


def test_happy_path_answers_without_tool(monkeypatch):
    insights.s3.get_object = MagicMock(side_effect=_fake_s3_get_object)
    insights._knowledge_cache["text"] = None
    insights._knowledge_cache["loaded_at"] = 0.0

    fake_resp = {
        "stopReason": "end_turn",
        "output": {"message": {"role": "assistant", "content": [{"text": "This app uses AgentCore Runtime."}]}},
    }
    insights.bedrock_runtime.converse = MagicMock(return_value=fake_resp)

    event = {"body": json.dumps({"message": "What runtime does the agent use?"})}
    result = insights.handler(event, FakeContext())
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["response"] == "This app uses AgentCore Runtime."
    assert body["tool_used"] is False

    system_prompt = insights.bedrock_runtime.converse.call_args.kwargs["system"][0]["text"]
    assert "Test knowledge" in system_prompt


def test_search_logs_tool_is_invoked_and_scoped(monkeypatch):
    insights.s3.get_object = MagicMock(side_effect=_fake_s3_get_object)
    insights._knowledge_cache["text"] = None
    insights._knowledge_cache["loaded_at"] = 0.0

    tool_use_resp = {
        "stopReason": "tool_use",
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": "t1", "name": "search_logs", "input": {"query": "guardrail_intervened"}}}],
            }
        },
    }
    final_resp = {
        "stopReason": "end_turn",
        "output": {"message": {"role": "assistant", "content": [{"text": "Found 1 matching log entry."}]}},
    }
    insights.bedrock_runtime.converse = MagicMock(side_effect=[tool_use_resp, final_resp])
    insights.logs.filter_log_events = MagicMock(
        return_value={"events": [{"timestamp": 1234567890000, "message": '{"guardrail_intervened": true}'}]}
    )

    event = {"body": json.dumps({"message": "Show me a guardrail intervention."})}
    result = insights.handler(event, FakeContext())
    body = json.loads(result["body"])
    assert body["tool_used"] is True
    assert body["response"] == "Found 1 matching log entry."

    called_groups = {c.kwargs["logGroupName"] for c in insights.logs.filter_log_events.call_args_list}
    assert called_groups == {
        "/aws/bedrock-agentcore/runtimes/policy_portal_agent-abc-prod",
        "/aws/lambda/insights-chat",
    }


def test_search_logs_direct_call_sorts_and_limits():
    insights.logs.filter_log_events = MagicMock(
        side_effect=[
            {"events": [{"timestamp": 100, "message": "old"}]},
            {"events": [{"timestamp": 300, "message": "new"}]},
        ]
    )
    result = insights.search_logs(query="", minutes_ago=60, limit=1)
    assert len(result["events"]) == 1
    assert result["events"][0]["message"] == "new"
