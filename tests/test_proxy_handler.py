import io
import json
import os
from unittest.mock import MagicMock

from tests.conftest import load_lambda_module

os.environ["AGENT_RUNTIME_ARN"] = "arn:aws:bedrock-agentcore:us-east-2:154833006816:runtime/policy_portal_agent-abc"
os.environ["AGENT_RUNTIME_ENDPOINT_NAME"] = "prod"

proxy = load_lambda_module(
    "proxy_handler", os.path.join(os.path.dirname(__file__), "..", "lambdas", "proxy", "handler.py")
)


class FakeContext:
    aws_request_id = "11111111-1111-1111-1111-111111111111"


def test_rejects_missing_auth_header():
    event = {"headers": {}, "body": json.dumps({"message": "hi"})}
    result = proxy.handler(event, FakeContext())
    assert result["statusCode"] == 401


def test_rejects_invalid_token(monkeypatch):
    from botocore.exceptions import ClientError

    error = ClientError({"Error": {"Code": "NotAuthorizedException", "Message": "Invalid token"}}, "GetUser")
    proxy.cognito.get_user = MagicMock(side_effect=error)
    event = {"headers": {"authorization": "Bearer bad-token"}, "body": json.dumps({"message": "hi"})}
    result = proxy.handler(event, FakeContext())
    assert result["statusCode"] == 401


def test_happy_path_invokes_agent(monkeypatch):
    proxy.cognito.get_user = MagicMock(return_value={"Username": "alice"})
    fake_body = io.BytesIO(json.dumps({"response": "Hello Alice", "session_id": "s1"}).encode())
    proxy.agentcore.invoke_agent_runtime = MagicMock(return_value={"response": fake_body})

    event = {
        "headers": {"authorization": "Bearer good-token"},
        "body": json.dumps({"message": "How much PTO do I have?"}),
    }
    result = proxy.handler(event, FakeContext())
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["response"] == "Hello Alice"

    call_kwargs = proxy.agentcore.invoke_agent_runtime.call_args.kwargs
    assert call_kwargs["qualifier"] == "prod"
    payload = json.loads(call_kwargs["payload"])
    assert payload["username"] == "alice"
    assert len(call_kwargs["runtimeSessionId"]) >= 33


def test_missing_message_returns_400():
    proxy.cognito.get_user = MagicMock(return_value={"Username": "alice"})
    event = {"headers": {"authorization": "Bearer good-token"}, "body": json.dumps({})}
    result = proxy.handler(event, FakeContext())
    assert result["statusCode"] == 400
