import json
import os
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from tests.conftest import load_lambda_module

STATE_TABLE = "test-model-selector-state"


@pytest.fixture
def ms_handler():
    with mock_aws():
        os.environ["RUNTIME_ID"] = "policy_portal_agent-abc123def0"
        os.environ["ENDPOINT_NAME"] = "prod"
        os.environ["DEFAULT_MODEL_ID"] = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        os.environ["STATE_TABLE"] = STATE_TABLE
        os.environ["APPCONFIG_APPLICATION_ID"] = "app123"
        os.environ["APPCONFIG_ENVIRONMENT_ID"] = "env123"
        os.environ["APPCONFIG_PROFILE_ID"] = "prof123"
        os.environ["APPCONFIG_STRATEGY_ID"] = "strat123"

        ddb = boto3.resource("dynamodb", region_name="us-east-2")
        ddb.create_table(
            TableName=STATE_TABLE,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        module = load_lambda_module(
            "model_selector_handler", os.path.join(os.path.dirname(__file__), "..", "lambdas", "model_selector", "handler.py")
        )
        # bedrock-agentcore-control and appconfig aren't moto-mocked here --
        # stub them directly, same convention as test_insights_chat_handler.py.
        module.appconfig.create_hosted_configuration_version = MagicMock(return_value={"VersionNumber": 1})
        module.appconfig.start_deployment = MagicMock(return_value={})
        yield module


class FakeContext:
    aws_request_id = "33333333-3333-3333-3333-333333333333"


def _get_event():
    return {"requestContext": {"http": {"method": "GET"}}}


def _post_event(body):
    return {"requestContext": {"http": {"method": "POST"}}, "body": json.dumps(body)}


def test_get_with_no_prior_state_returns_defaults(ms_handler):
    result = ms_handler.handler(_get_event(), FakeContext())
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["weights"] == {"quality": 50, "cost": 25, "latency": 25}
    assert body["current_model"] == "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    assert len(body["model_candidates"]) == 5


def test_options_request_returns_200(ms_handler):
    event = {"requestContext": {"http": {"method": "OPTIONS"}}}
    result = ms_handler.handler(event, FakeContext())
    assert result["statusCode"] == 200


def test_normalize_weights_rescales_to_100(ms_handler):
    assert ms_handler._normalize_weights({"quality": 60, "cost": 30, "latency": 30}) == {
        "quality": 50, "cost": 25, "latency": 25,
    }


def test_evaluate_quality_heavy_picks_opus_and_does_not_switch_if_no_change(ms_handler, monkeypatch):
    # All 5 candidates "equally fast" so latency doesn't affect ranking --
    # isolates the quality-weighted comparison.
    ms_handler._measure_latency_scores = MagicMock(return_value={c["id"]: 100 for c in ms_handler.MODEL_CANDIDATES})
    # Seed current_model as Opus already, so a quality-heavy evaluate should
    # NOT attempt a live switch (winner == current already).
    ms_handler.state_table.put_item(Item={
        "id": "state", "weights": {"quality": 50, "cost": 25, "latency": 25},
        "ranking": [], "current_model": "us.anthropic.claude-opus-4-5-20251101-v1:0", "last_updated": "0",
    })
    ms_handler.control.get_agent_runtime = MagicMock()
    ms_handler.control.update_agent_runtime = MagicMock()

    event = _post_event({"weights": {"quality": 100, "cost": 0, "latency": 0}})
    result = ms_handler.handler(event, FakeContext())
    body = json.loads(result["body"])

    assert body["ranking"][0]["id"] == "us.anthropic.claude-opus-4-5-20251101-v1:0"
    assert body["switched"] is False
    assert body["current_model"] == "us.anthropic.claude-opus-4-5-20251101-v1:0"
    ms_handler.control.update_agent_runtime.assert_not_called()


def test_evaluate_cost_heavy_picks_nova_micro_and_switches(ms_handler):
    ms_handler._measure_latency_scores = MagicMock(return_value={c["id"]: 100 for c in ms_handler.MODEL_CANDIDATES})
    ms_handler.state_table.put_item(Item={
        "id": "state", "weights": {"quality": 50, "cost": 25, "latency": 25},
        "ranking": [], "current_model": "us.anthropic.claude-sonnet-4-5-20250929-v1:0", "last_updated": "0",
    })
    ms_handler.control.get_agent_runtime = MagicMock(return_value={
        "agentRuntimeArtifact": {"codeConfiguration": {}},
        "roleArn": "arn:aws:iam::154833006816:role/fake",
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {"securityGroups": ["sg-fake"], "subnets": ["subnet-fake1", "subnet-fake2"], "requireServiceS3Endpoint": False},
        },
        "environmentVariables": {"MODEL_ID": "us.anthropic.claude-sonnet-4-5-20250929-v1:0"},
        "status": "READY",
        "agentRuntimeVersion": "2",
    })
    ms_handler.control.update_agent_runtime = MagicMock(return_value={"agentRuntimeVersion": "3"})
    ms_handler.control.update_agent_runtime_endpoint = MagicMock(return_value={})
    ms_handler.control.get_agent_runtime_endpoint = MagicMock(return_value={"status": "READY"})

    event = _post_event({"weights": {"quality": 0, "cost": 100, "latency": 0}})
    result = ms_handler.handler(event, FakeContext())
    body = json.loads(result["body"])

    assert body["ranking"][0]["id"] == "us.amazon.nova-micro-v1:0"
    assert body["switched"] is True
    assert body["current_model"] == "us.amazon.nova-micro-v1:0"
    ms_handler.control.update_agent_runtime.assert_called_once()
    called_env = ms_handler.control.update_agent_runtime.call_args.kwargs["environmentVariables"]
    assert called_env["MODEL_ID"] == "us.amazon.nova-micro-v1:0"
    ms_handler.control.update_agent_runtime_endpoint.assert_called_once()


def test_measure_latency_scores_handles_a_failing_candidate(ms_handler):
    def fake_converse(modelId, **kwargs):
        if modelId == "us.amazon.nova-micro-v1:0":
            raise RuntimeError("boom")
        return {}
    ms_handler.bedrock_runtime.converse = MagicMock(side_effect=fake_converse)

    scores = ms_handler._measure_latency_scores()
    assert scores["us.amazon.nova-micro-v1:0"] == 0
    assert scores["us.anthropic.claude-opus-4-5-20251101-v1:0"] > 0
