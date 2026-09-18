import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests.conftest import load_lambda_module

LEAVE_TABLE = "test-leave-balance"
REQUESTS_TABLE = "test-requests"


@pytest.fixture
def tools_handler():
    with mock_aws():
        os.environ["LEAVE_BALANCE_TABLE"] = LEAVE_TABLE
        os.environ["REQUESTS_TABLE"] = REQUESTS_TABLE

        ddb = boto3.resource("dynamodb", region_name="us-east-2")
        ddb.create_table(
            TableName=LEAVE_TABLE,
            KeySchema=[{"AttributeName": "username", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "username", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName=REQUESTS_TABLE,
            KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "request_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.Table(LEAVE_TABLE).put_item(
            Item={"username": "alice", "pto_days_remaining": 12, "sick_days_remaining": 5, "as_of": "2026-09-01"}
        )

        module = load_lambda_module(
            "tools_handler", os.path.join(os.path.dirname(__file__), "..", "lambdas", "tools", "handler.py")
        )
        yield module


def test_check_leave_balance_found(tools_handler):
    event = {"bedrockAgentCoreToolName": "policy-portal-tools___check_leave_balance", "username": "alice"}
    result = tools_handler.handler(event, None)
    assert result["username"] == "alice"
    assert float(result["pto_days_remaining"]) == 12


def test_check_leave_balance_not_found(tools_handler):
    event = {"bedrockAgentCoreToolName": "policy-portal-tools___check_leave_balance", "username": "nobody"}
    result = tools_handler.handler(event, None)
    assert "error" in result


def test_submit_request_writes_item(tools_handler):
    event = {
        "bedrockAgentCoreToolName": "policy-portal-tools___submit_request",
        "username": "alice",
        "request_type": "PTO",
        "start_date": "2026-10-01",
        "end_date": "2026-10-03",
        "reason": "vacation",
    }
    result = tools_handler.handler(event, None)
    assert result["status"] == "PENDING"
    assert "request_id" in result


def test_unknown_tool_returns_error(tools_handler):
    event = {"bedrockAgentCoreToolName": "policy-portal-tools___delete_everything", "username": "alice"}
    result = tools_handler.handler(event, None)
    assert "error" in result
