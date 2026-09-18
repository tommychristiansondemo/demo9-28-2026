import concurrent.futures
import json
import os
import time
from decimal import Decimal

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-2")
RUNTIME_ID = os.environ["RUNTIME_ID"]
ENDPOINT_NAME = os.environ["ENDPOINT_NAME"]
DEFAULT_MODEL_ID = os.environ["DEFAULT_MODEL_ID"]
STATE_TABLE = os.environ["STATE_TABLE"]
APPCONFIG_APPLICATION_ID = os.environ["APPCONFIG_APPLICATION_ID"]
APPCONFIG_ENVIRONMENT_ID = os.environ["APPCONFIG_ENVIRONMENT_ID"]
APPCONFIG_PROFILE_ID = os.environ["APPCONFIG_PROFILE_ID"]
APPCONFIG_STRATEGY_ID = os.environ["APPCONFIG_STRATEGY_ID"]

bedrock_runtime = boto3.client("bedrock-runtime", region_name=REGION, config=Config(read_timeout=30, connect_timeout=10))
control = boto3.client("bedrock-agentcore-control", region_name=REGION)
appconfig = boto3.client("appconfig", region_name=REGION)
ddb = boto3.resource("dynamodb", region_name=REGION)
state_table = ddb.Table(STATE_TABLE)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "content-type",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
}

STATE_ITEM_ID = "state"

# Illustrative, documented reference values -- NOT live-benchmarked (there's
# no fast way to benchmark "quality" in a button click). latency_score is the
# one dimension actually measured live below, via real concurrent Converse
# calls. Same disclosed design as the precedent this feature is modeled on
# (see docs/DESIGN.md and projectknowledge.md for the full "what's real vs
# illustrative" callout). Real spread across 2 vendors so weighting choices
# produce genuinely different winners.
MODEL_CANDIDATES = [
    {"id": "us.anthropic.claude-opus-4-5-20251101-v1:0", "label": "Claude Opus 4.5", "quality_score": 100, "cost_score": 20},
    {"id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0", "label": "Claude Sonnet 4.5", "quality_score": 85, "cost_score": 55},
    {"id": "us.anthropic.claude-haiku-4-5-20251001-v1:0", "label": "Claude Haiku 4.5", "quality_score": 65, "cost_score": 80},
    {"id": "us.amazon.nova-pro-v1:0", "label": "Amazon Nova Pro", "quality_score": 70, "cost_score": 75},
    {"id": "us.amazon.nova-micro-v1:0", "label": "Amazon Nova Micro", "quality_score": 45, "cost_score": 100},
]


def _response(status, body):
    return {"statusCode": status, "headers": {**CORS_HEADERS, "Content-Type": "application/json"}, "body": json.dumps(body, default=str)}


def _normalize_weights(raw: dict) -> dict:
    q = max(0, min(100, int(raw.get("quality", 50))))
    c = max(0, min(100, int(raw.get("cost", 25))))
    l = max(0, min(100, int(raw.get("latency", 25))))
    total = q + c + l
    if total == 0:
        return {"quality": 50, "cost": 25, "latency": 25}
    # Rescale to sum exactly 100, in case the client sent something slightly
    # off (rounding on the frontend's proportional-redistribution sliders).
    return {
        "quality": round(q * 100 / total),
        "cost": round(c * 100 / total),
        "latency": round(l * 100 / total),
    }


def _num(value):
    """DynamoDB returns Decimal for all numbers -- convert back to int/float
    so the JSON response carries real numbers, not stringified Decimals."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_num(v) for v in value]
    if isinstance(value, dict):
        return {k: _num(v) for k, v in value.items()}
    return value


def _load_state() -> dict:
    resp = state_table.get_item(Key={"id": STATE_ITEM_ID})
    item = resp.get("Item")
    if not item:
        return {
            "weights": {"quality": 50, "cost": 25, "latency": 25},
            "ranking": _rank(MODEL_CANDIDATES, {"quality": 50, "cost": 25, "latency": 25}, {}),
            "current_model": DEFAULT_MODEL_ID,
            "last_updated": None,
        }
    return {
        "weights": _num(item.get("weights", {"quality": 50, "cost": 25, "latency": 25})),
        "ranking": _num(item.get("ranking", [])),
        "current_model": item.get("current_model", DEFAULT_MODEL_ID),
        "last_updated": item.get("last_updated"),
    }


def _save_state(weights: dict, ranking: list, current_model: str) -> None:
    # DynamoDB's boto3 resource layer rejects native Python floats (only
    # Decimal/int) -- _rank()'s final_score is a float. Round-trip through
    # json with parse_float=Decimal rather than hand-converting every
    # numeric field.
    ranking_decimal = json.loads(json.dumps(ranking), parse_float=Decimal)
    state_table.put_item(Item={
        "id": STATE_ITEM_ID,
        "weights": weights,
        "ranking": ranking_decimal,
        "current_model": current_model,
        "last_updated": str(int(time.time())),
    })


def _measure_latency_scores() -> dict:
    """5 concurrent Converse calls, timed. Fastest scores 100; others scaled
    by the ratio of the fastest time to their own (a model 2x slower than the
    fastest scores 50). A candidate that errors scores 0 rather than
    aborting the whole evaluation.
    """
    def _time_one(model_id):
        start = time.time()
        try:
            bedrock_runtime.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": "Reply with just the word OK."}]}],
                inferenceConfig={"maxTokens": 10, "temperature": 0},
            )
            return model_id, time.time() - start, None
        except Exception as e:  # noqa: BLE001
            return model_id, None, str(e)

    times = {}
    errors = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(MODEL_CANDIDATES)) as pool:
        for model_id, elapsed, err in pool.map(_time_one, [c["id"] for c in MODEL_CANDIDATES]):
            if elapsed is not None:
                times[model_id] = elapsed
            else:
                errors[model_id] = err

    if not times:
        return {c["id"]: 0 for c in MODEL_CANDIDATES}

    fastest = min(times.values())
    scores = {}
    for c in MODEL_CANDIDATES:
        mid = c["id"]
        if mid in times:
            scores[mid] = round(100 * fastest / times[mid])
        else:
            scores[mid] = 0
    return scores


def _rank(candidates: list, weights: dict, latency_scores: dict) -> list:
    ranking = []
    for c in candidates:
        latency_score = latency_scores.get(c["id"], 0)
        final = (
            weights["quality"] / 100 * c["quality_score"]
            + weights["cost"] / 100 * c["cost_score"]
            + weights["latency"] / 100 * latency_score
        )
        ranking.append({
            "id": c["id"],
            "label": c["label"],
            "quality_score": c["quality_score"],
            "cost_score": c["cost_score"],
            "latency_score": latency_score,
            "final_score": round(final, 1),
        })
    ranking.sort(key=lambda r: r["final_score"], reverse=True)
    return ranking


def _publish_weights_to_appconfig(weights: dict) -> None:
    """Weights are genuinely stored in AppConfig -- this is the write path
    Tommy asked for. The zero-final-bake-time custom deployment strategy
    (see model_selector_stack.py) is required: AppConfig's predefined
    AllAtOnce strategy has a hidden 10-minute FinalBakeTimeInMinutes that
    would block back-to-back weight-saves during a live class demo.
    """
    version = appconfig.create_hosted_configuration_version(
        ApplicationId=APPCONFIG_APPLICATION_ID,
        ConfigurationProfileId=APPCONFIG_PROFILE_ID,
        Content=json.dumps(weights).encode("utf-8"),
        ContentType="application/json",
    )
    appconfig.start_deployment(
        ApplicationId=APPCONFIG_APPLICATION_ID,
        EnvironmentId=APPCONFIG_ENVIRONMENT_ID,
        DeploymentStrategyId=APPCONFIG_STRATEGY_ID,
        ConfigurationProfileId=APPCONFIG_PROFILE_ID,
        ConfigurationVersion=str(version["VersionNumber"]),
    )


def _switch_live_model(new_model_id: str) -> dict:
    """Synchronous two-phase update: UpdateAgentRuntime (async, 30-90+s to
    reach READY) then UpdateAgentRuntimeEndpoint (moves live traffic).
    Firing both back-to-back without waiting throws ConflictException --
    confirmed in a prior class's ModelSelector feature. This is a
    user-initiated, one-shot button click (not a continuous poller), so
    polling synchronously inside this one Lambda invocation is the right
    shape -- the Function URL allows up to 15 minutes, unlike API Gateway's
    hard 29s ceiling.
    """
    current = control.get_agent_runtime(agentRuntimeId=RUNTIME_ID)
    env_vars = dict(current.get("environmentVariables", {}))
    env_vars["MODEL_ID"] = new_model_id

    # GetAgentRuntime echoes back a `requireServiceS3Endpoint` field inside
    # networkModeConfig that CDK never set explicitly -- confirmed live that
    # resubmitting it verbatim on UpdateAgentRuntime fails with
    # "Agents created after 2026-06-08T00:00:00Z cannot modify
    # requireServiceS3Endpoint", even when the value is unchanged. Rebuild
    # networkConfiguration from only the fields CDK originally set rather
    # than passing the GET response through untouched.
    net_in = current["networkConfiguration"]
    network_configuration = {
        "networkMode": net_in["networkMode"],
        "networkModeConfig": {
            "securityGroups": net_in["networkModeConfig"]["securityGroups"],
            "subnets": net_in["networkModeConfig"]["subnets"],
        },
    }

    update_resp = control.update_agent_runtime(
        agentRuntimeId=RUNTIME_ID,
        agentRuntimeArtifact=current["agentRuntimeArtifact"],
        roleArn=current["roleArn"],
        networkConfiguration=network_configuration,
        environmentVariables=env_vars,
    )
    new_version = update_resp["agentRuntimeVersion"]

    deadline = time.time() + 300
    status = "UPDATING"
    while time.time() < deadline:
        resp = control.get_agent_runtime(agentRuntimeId=RUNTIME_ID, agentRuntimeVersion=new_version)
        status = resp["status"]
        if status == "READY":
            break
        if status in ("CREATE_FAILED", "UPDATE_FAILED"):
            return {"switched": False, "error": f"Runtime update failed: {status}"}
        time.sleep(5)
    else:
        return {"switched": False, "error": f"Timed out waiting for runtime version {new_version} to reach READY (last status {status})"}

    control.update_agent_runtime_endpoint(
        agentRuntimeId=RUNTIME_ID,
        endpointName=ENDPOINT_NAME,
        agentRuntimeVersion=new_version,
    )

    deadline = time.time() + 120
    status = "UPDATING"
    while time.time() < deadline:
        resp = control.get_agent_runtime_endpoint(agentRuntimeId=RUNTIME_ID, endpointName=ENDPOINT_NAME)
        status = resp["status"]
        if status == "READY":
            return {"switched": True}
        if status in ("CREATE_FAILED", "UPDATE_FAILED"):
            return {"switched": False, "error": f"Endpoint update failed: {status}"}
        time.sleep(5)

    return {"switched": False, "error": f"Timed out waiting for endpoint to move to version {new_version} (last status {status})"}


def _handle_get():
    state = _load_state()
    return _response(200, {
        "weights": state["weights"],
        "model_candidates": MODEL_CANDIDATES,
        "ranking": state["ranking"],
        "current_model": state["current_model"],
        "last_updated": state["last_updated"],
    })


def _handle_post(body: dict):
    weights = _normalize_weights(body.get("weights", {}))

    _publish_weights_to_appconfig(weights)

    latency_scores = _measure_latency_scores()
    ranking = _rank(MODEL_CANDIDATES, weights, latency_scores)
    winner = ranking[0]["id"]

    state = _load_state()
    previous_model = state["current_model"]

    switch_result = {"switched": False}
    if winner != previous_model:
        switch_result = _switch_live_model(winner)

    current_model = winner if switch_result.get("switched") else previous_model
    _save_state(weights, ranking, current_model)

    return _response(200, {
        "weights": weights,
        "ranking": ranking,
        "previous_model": previous_model,
        "current_model": current_model,
        "switched": switch_result.get("switched", False),
        "switch_error": switch_result.get("error"),
    })


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    if method == "OPTIONS":
        return _response(200, {})
    if method == "GET":
        return _handle_get()
    if method == "POST":
        try:
            body = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            return _response(400, {"error": "Invalid JSON body"})
        return _handle_post(body)
    return _response(405, {"error": "Method not allowed"})
