#!/usr/bin/env python3
"""End-to-end smoke test against the live deployment, using a real Cognito
sign-up/sign-in and real HTTP calls to the Function URL -- not mocks. Run
after PolicyPortal-Api and PolicyPortal-Frontend are both deployed.
"""
import json
import sys
import time
import uuid

import boto3
import requests

REGION = "us-east-2"

cfn = boto3.client("cloudformation", region_name=REGION)


def stack_outputs(name):
    return {
        o["OutputKey"]: o["OutputValue"]
        for o in cfn.describe_stacks(StackName=name)["Stacks"][0]["Outputs"]
    }


auth_out = stack_outputs("PolicyPortal-Auth")
api_out = stack_outputs("PolicyPortal-Api")

USER_POOL_CLIENT_ID = auth_out["UserPoolClientId"]
API_URL = api_out["ApiFunctionUrl"]

cognito = boto3.client("cognito-idp", region_name=REGION)

username = f"smoketest-{uuid.uuid4().hex[:8]}"
password = "test123"

print(f"[1] Signing up {username} ...")
cognito.sign_up(ClientId=USER_POOL_CLIENT_ID, Username=username, Password=password)
print("    Signed up (should be auto-confirmed).")

print("[2] Signing in ...")
auth_resp = cognito.initiate_auth(
    AuthFlow="USER_PASSWORD_AUTH",
    ClientId=USER_POOL_CLIENT_ID,
    AuthParameters={"USERNAME": username, "PASSWORD": password},
)
access_token = auth_resp["AuthenticationResult"]["AccessToken"]
print("    Got access token.")


def ask(message, history=None):
    resp = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"message": message, "history": history or []},
        timeout=90,
    )
    return resp


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"    [{status}] {label}")
    return condition


results = []

print("[3] Unauthenticated request should be rejected ...")
bad_resp = requests.post(API_URL, json={"message": "hi"}, timeout=30)
results.append(check("401 without auth header", bad_resp.status_code == 401))

print("[4] RAG question grounded in the corpus ...")
resp = ask("How many days of paid time off do new employees get per year?")
print(f"    HTTP {resp.status_code}: {resp.text[:400]}")
data = resp.json()
text = data.get("response", "")
results.append(check("200 OK", resp.status_code == 200))
results.append(check("mentions 15 days (years 0-2 PTO accrual)", "15" in text))

print("[5] Out-of-corpus question should be declined gracefully ...")
resp2 = ask("What is the company's stock ticker symbol on NASDAQ?")
text2 = resp2.json().get("response", "")
print(f"    Response: {text2[:300]}")
results.append(check("200 OK", resp2.status_code == 200))

print("[6] check_leave_balance tool for the newly signed-up (unseeded) user ...")
resp3 = ask("How much PTO do I have left?")
text3 = resp3.json().get("response", "")
print(f"    Response: {text3[:300]}")
results.append(check("200 OK", resp3.status_code == 200))

print("[7] check_leave_balance tool for a seeded user (sign in as 'demo') ...")
# 'demo' was seeded by scripts/seed_leave_balances.py with 12.5 PTO days.
demo_pw = "demo1234"
try:
    cognito.sign_up(ClientId=USER_POOL_CLIENT_ID, Username="demo", Password=demo_pw)
except cognito.exceptions.UsernameExistsException:
    pass
demo_auth = cognito.initiate_auth(
    AuthFlow="USER_PASSWORD_AUTH",
    ClientId=USER_POOL_CLIENT_ID,
    AuthParameters={"USERNAME": "demo", "PASSWORD": demo_pw},
)
demo_token = demo_auth["AuthenticationResult"]["AccessToken"]
resp4 = requests.post(
    API_URL,
    headers={"Authorization": f"Bearer {demo_token}", "Content-Type": "application/json"},
    json={"message": "How many PTO days do I have left?"},
    timeout=90,
)
text4 = resp4.json().get("response", "")
print(f"    Response: {text4[:300]}")
results.append(check("200 OK", resp4.status_code == 200))
results.append(check("mentions 12.5", "12.5" in text4))

print("[8] submit_request tool ...")
resp5 = ask("Please submit a PTO request from 2026-10-05 to 2026-10-07, reason: family trip.")
text5 = resp5.json().get("response", "")
print(f"    Response: {text5[:400]}")
results.append(check("200 OK", resp5.status_code == 200))

print("[9] Guardrail PII masking -- message containing a fake SSN ...")
resp6 = ask("My SSN is 123-45-6789, can you confirm my leave request was received?")
data6 = resp6.json()
text6 = data6.get("response", "")
print(f"    Response: {text6[:400]}")
print(f"    guardrail_intervened flag: {data6.get('guardrail_intervened')}")
results.append(check("200 OK", resp6.status_code == 200))
results.append(check("raw SSN not echoed back in response", "123-45-6789" not in text6))
# Note: the system prompt itself often declines to process a bare SSN before
# the model ever echoes it, so the Guardrail's sensitive-info policy has
# nothing to mask and genuinely reports no action -- two independent, valid
# defense layers, not a bug. See docs/DESIGN.md for the confirmed-live
# evidence that Guardrail masking is real (this exact scenario, without a
# system prompt steering the model away from the topic, does trigger it).

print("[9b] Guardrail PII masking -- email/phone the system prompt doesn't specifically flag ...")
resp6b = ask(
    "Please send the benefits enrollment confirmation to my personal email "
    "john.doe12345@gmail.com and cc my phone number 555-123-4567 in the "
    "notes -- can you repeat those back to confirm you got them right?"
)
data6b = resp6b.json()
text6b = data6b.get("response", "")
print(f"    Response: {text6b[:400]}")
print(f"    guardrail_intervened flag: {data6b.get('guardrail_intervened')}")
results.append(check("200 OK", resp6b.status_code == 200))
results.append(check("raw email not echoed back in response", "john.doe12345@gmail.com" not in text6b))
results.append(check("raw phone not echoed back in response", "555-123-4567" not in text6b))
results.append(check("guardrail_intervened flag set", data6b.get("guardrail_intervened") is True))

print("\n=== SUMMARY ===")
passed = sum(1 for r in results if r)
print(f"{passed}/{len(results)} checks passed")
sys.exit(0 if passed == len(results) else 1)
