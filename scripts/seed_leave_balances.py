#!/usr/bin/env python3
"""Seeds a few synthetic leave-balance records so the demo has something to
check on day one. Run after PolicyPortal-Data is deployed. Usernames here
are examples only -- any student's real signup username won't be present
until they sign up, so the agent should report "no record found" for those
gracefully (confirmed behavior in lambdas/tools/handler.py).
"""
import boto3
import sys
from decimal import Decimal

REGION = "us-east-2"

cfn = boto3.client("cloudformation", region_name=REGION)
outputs = {
    o["OutputKey"]: o["OutputValue"]
    for o in cfn.describe_stacks(StackName="PolicyPortal-Data")["Stacks"][0]["Outputs"]
}
table_name = outputs["LeaveBalanceTableName"]

ddb = boto3.resource("dynamodb", region_name=REGION)
table = ddb.Table(table_name)

SEED = [
    {"username": "demo", "pto_days_remaining": Decimal("12.5"), "sick_days_remaining": Decimal("6"), "as_of": "2026-09-18"},
    {"username": "alice", "pto_days_remaining": Decimal("18"), "sick_days_remaining": Decimal("10"), "as_of": "2026-09-18"},
    {"username": "bob", "pto_days_remaining": Decimal("3.5"), "sick_days_remaining": Decimal("2"), "as_of": "2026-09-18"},
]

for item in SEED:
    table.put_item(Item=item)
    print(f"Seeded leave balance for {item['username']}")

print(f"Done. Table: {table_name}")
