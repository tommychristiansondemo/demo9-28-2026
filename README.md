# Policy & Benefits Assistant

A unified, secure portal demonstrating enterprise Amazon Bedrock AgentCore
patterns: RAG over policy documents, an "Action Groups"-equivalent mock
secure API (leave balance / request submission) via AgentCore Gateway, and
real Guardrails-based PII masking. See `docs/DESIGN.md` for the full
architecture and the "what's real vs. simulated" disclosures.

**Live at:** https://www.awsteach.com
Account `154833006816`, region `us-east-2` (+ `us-east-1` for the
CloudFront/ACM certificate only).

## Deploying from scratch

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Both regions need bootstrapping (us-east-1 only for the CloudFront cert).
cdk bootstrap aws://<account>/us-east-2
cdk bootstrap aws://<account>/us-east-1

# Phase 1: everything except AgentRuntime/Api. app.py auto-skips those two
# stacks until the agent code zip exists in S3 -- this is expected on a
# first run, not an error.
cdk deploy PolicyPortal-Network PolicyPortal-Auth PolicyPortal-Data \
  PolicyPortal-Guardrail PolicyPortal-KnowledgeBase PolicyPortal-Gateway \
  PolicyPortal-Cert PolicyPortal-Frontend --require-approval never

python3 scripts/seed_leave_balances.py

# Phase 2: package + upload the AgentCore Runtime code, then deploy the
# stacks that depend on it.
bash scripts/package_agent.sh
cdk deploy PolicyPortal-AgentRuntime PolicyPortal-Api --require-approval never

# Frontend needs the API URL, which only exists after PolicyPortal-Api.
bash scripts/deploy_frontend.sh
```

## Redeploying after a change

- **Backend stack change:** `cdk deploy <StackName>` as usual.
- **Agent code change (`agents/policy_agent/agent.py`):** re-run
  `scripts/package_agent.sh` then `cdk deploy PolicyPortal-AgentRuntime`
  (the S3 object VersionId is read at synth time and pinned into the
  Runtime's code config, so CDK will see a real property change).
- **Frontend-only change:** `bash scripts/deploy_frontend.sh`. No backend
  redeploy needed -- the frontend reads `/config.json` at runtime.

## Verifying

```bash
AWS_DEFAULT_REGION=us-east-2 python3 -m pytest tests/ -v   # unit tests (moto-mocked)
python3 scripts/smoke_test.py                                # live end-to-end
```

`smoke_test.py` signs up a real Cognito user, signs in, and drives the live
API: unauthenticated rejection, a RAG question grounded in the corpus, an
out-of-corpus question, `check_leave_balance` for both an unseeded and a
seeded user, `submit_request`, and a PII/Guardrail check (a fake SSN in the
message, confirming it isn't echoed back and the Guardrail's own trace
flagged an intervention).

## Tearing down

This app was left **LIVE** intentionally -- it's mid-series, not the end of
the class rotation, so it was not torn down (unlike the prior two classes in
this series, which were fully torn down including CDK bootstrap at the end
of their respective classes). When Tommy is ready to tear it down:

```bash
cd /home/tommy/demo && source .venv/bin/activate
cdk destroy --all --force
```

Then clean up what CDK doesn't own (same pattern as the prior two classes'
teardowns -- see `agentcore_unified_portal_demo.md` in the assistant's
memory for the exact prior-class sequences to reuse):
- Orphaned `/aws/lambda/PolicyPortal-*` and
  `/aws/bedrock-agentcore/runtimes/policy_portal_agent-*` CloudWatch log
  groups (`aws logs delete-log-group`).
- Any leftover ACM-validation CNAME under `www.awsteach.com` in Route 53
  (harmless to leave, per precedent, but can be removed).
- Only remove the `CDKToolkit` bootstrap stacks in both regions if this is
  genuinely the end of the class series and Tommy wants the account reset
  to a from-scratch state for the next class -- do not do this by default.
