import os

from tests.conftest import load_lambda_module

presignup = load_lambda_module(
    "presignup_handler", os.path.join(os.path.dirname(__file__), "..", "lambdas", "presignup", "handler.py")
)


def test_autoconfirms_user():
    event = {"response": {}, "request": {"userAttributes": {}}}
    result = presignup.handler(event, None)
    assert result["response"]["autoConfirmUser"] is True


def test_autoverifies_email_when_present():
    event = {"response": {}, "request": {"userAttributes": {"email": "a@example.com"}}}
    result = presignup.handler(event, None)
    assert result["response"]["autoVerifyEmail"] is True
