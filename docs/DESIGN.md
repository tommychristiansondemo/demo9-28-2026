# Policy & Benefits Assistant — Design

Fourth class demo in this series ("Building Agentic AI with Amazon Bedrock
AgentCore"), following the United Airlines IROPS demo, the Cisco Network
Incident Remediation Assistant, and the Evernorth RAG Benefits Assistant.
Live at **https://www.awsteach.com**, account `154833006816`, region
**us-east-2** (+ us-east-1 for the CloudFront/ACM certificate only).

A single, unified portal (deliberately *not* the two-portal pattern used in
the prior three classes — see the project memory for why) where a signed-in
employee can ask questions grounded in real organizational policy documents,
check their own leave balance, and submit a formal leave/benefits request.

## Enterprise features demonstrated

1. **RAG** — a Bedrock Knowledge Base (S3 Vectors backend) over 5 synthetic
   policy documents (`knowledge/corpus/`): leave policy, benefits handbook,
   code of conduct, remote work policy, data handling/compliance policy.
   The agent calls `bedrock-agent-runtime:Retrieve` explicitly as its own
   tool (`retrieve_policy_docs`) rather than using `RetrieveAndGenerate`, so
   retrieval is a visible, inspectable step for teaching.
2. **"Action Groups" equivalent** — classic Bedrock Agents has been
   confirmed in Maintenance Mode ("new agent creation is not available for
   accounts without prior service usage") in this exact account across two
   prior classes, so this uses **AgentCore Gateway** (MCP, `AWS_IAM`
   authorizer) → a Lambda target exposing `check_leave_balance` and
   `submit_request` as tools, backed by two DynamoDB tables. Functionally
   identical to what Action Groups would provide, and a better fit for a
   class about AgentCore specifically.
3. **Guardrails for Amazon Bedrock** — a real `AWS::Bedrock::Guardrail`
   applies PII anonymization (SSN, email, phone, name, address, card/bank
   numbers, age) and standard content filters (hate, insults, sexual,
   violence, misconduct, prompt-attack) on every `Converse` call the agent
   makes. This is the one feature none of the prior three class demos used.

## Architecture

- **AgentCore Runtime** (direct-code S3 zip deploy, ARM64, no ECR/Docker)
  runs `agents/policy_agent/agent.py` — a manual boto3 `Converse` tool-use
  loop (not the Strands SDK), chosen specifically so the Guardrail
  parameters are guaranteed to be passed on every model call rather than
  hidden inside a framework's own loop.
- **AgentCore Gateway** exposes the mock leave/benefits API as MCP tools;
  the agent connects via `mcp_proxy_for_aws.client.aws_iam_streamablehttp_client`
  (per-request SigV4 signing).
- **Cognito** — zero-friction self-signup (6-char min password, no
  complexity rules, PreSignUp Lambda auto-confirms), same pattern as all
  three prior classes.
- **API** — a Lambda Function URL (`auth_type=NONE`), not API Gateway. A
  live multi-tool agentic turn can exceed API Gateway's hard, non-
  configurable 29-second integration timeout (confirmed in two prior
  classes). The proxy Lambda's only job is
  `bedrock-agentcore:InvokeAgentRuntime`; identity is validated via
  `cognito-idp:GetUser` on the caller's **access token** (not ID token).
- **Network** — VPC-isolated Lambdas (no NAT/IGW), PrivateLink interface
  endpoints for `bedrock-runtime`, `bedrock-agent-runtime`,
  `bedrock-agentcore`, `bedrock-agentcore-gateway`, `logs`, `sts`,
  `cognito-idp`, plus S3 and DynamoDB gateway endpoints.
- **Frontend** — a lean React SPA (no Amplify; `@aws-sdk/client-cognito-identity-provider`
  directly for sign-up/sign-in) on S3 + CloudFront (OAC) + ACM (us-east-1)
  + Route 53 alias at `www.awsteach.com`. Reads a runtime `/config.json` so
  a backend redeploy never requires a frontend rebuild.

## What's real vs. simulated

- The leave balances and submitted requests are **real DynamoDB records**,
  written and read live by the Gateway's Lambda target — not hardcoded in
  the agent or the frontend. Only the underlying "employees" are synthetic
  seed data (`scripts/seed_leave_balances.py`).
- The Guardrail is a **real Bedrock Guardrail**, actually evaluated on every
  `Converse` call (`guardrailConfig` with `trace: "enabled"`); the proxy
  response includes a `guardrail_intervened` flag derived from the actual
  guardrail trace returned by the API, not a heuristic over the response
  text.
- **CloudTrail caveat** (inherited lesson from the Evernorth class): if
  CloudTrail auditing is ever added on top of this app, remember it covers
  `Converse`/`InvokeModel` as management events but **not**
  `bedrock-agent-runtime:Retrieve` — the retrieval half of RAG leaves no
  CloudTrail record. This app does not currently have CloudTrail wired in
  at all (out of scope unless asked).
- Request approval is simulated: `submit_request` always writes status
  `PENDING` — there's no actual approval workflow behind it.

## Real deploy-only bugs found building this (beyond the prior-class list carried into the build prompt)

- **`AWS::Bedrock::Guardrail`'s `PROMPT_ATTACK` content filter type only
  supports input-side filtering** — passing any `output_strength` other
  than `NONE` for `PROMPT_ATTACK` fails with `"PROMPT ATTACK content filter
  strength for response must be NONE"`. Every other filter type accepts
  `MEDIUM`/`MEDIUM` for both directions.
- **`aws-cdk-lib` 2.270's `s3.Bucket.bucket_name` always returns a CDK
  Token string, even when an explicit literal `bucket_name=` was passed
  in.** This breaks any Python-side (not CloudFormation-side) code that
  needs the real bucket name at `cdk synth` time — e.g. this app's
  two-phase "does the agent code zip already exist in S3" check, which
  runs a real `boto3.head_object` *during app.py's own execution*, before
  any CloudFormation template is even built. Fixed by keeping the literal
  name as a separate plain Python attribute (`DataStack.agent_code_bucket_name`)
  rather than reading it back off the construct.
- **`CfnRuntime.S3LocationProperty` uses a field named `prefix`, not
  `key`**, for the S3 object key of the direct-code-deploy artifact — an
  easy mismatch to guess wrong coming from other AWS S3-location shapes
  that use `key`.
- A **leftover `CDKToolkit` bootstrap stack was found `DELETE_FAILED` in
  us-east-1** at the start of this build (`ContainerAssetsRepository`
  still had 10 images, blocking stack deletion), from an earlier,
  differently-incomplete teardown not documented in this account's class
  history. Cleared by batch-deleting the images and retrying
  `delete-stack`, then re-running `cdk bootstrap`. Not caused by this
  build, but blocked it until resolved — worth a stray-resource sweep
  before `cdk bootstrap` if bootstrapping a region that's hosted prior
  classes.
- Knowledge Base, Gateway, and the ingestion custom resource all deployed
  cleanly on the **first attempt** with no new bugs — the defensive
  patterns carried over from the prior three classes (KB role
  `DefaultPolicy` dependency, S3 Vectors non-filterable metadata keys,
  `GATEWAY_IAM_ROLE` credential provider type, `attr_gateway_identifier`
  not `attr_gateway_id`) held up.

**More real bugs found only through live end-to-end verification (the app
deployed cleanly, but every one of these only surfaced once real traffic hit
it):**

- **`mcp_proxy_for_aws.client.aws_iam_streamablehttp_client`'s actual keyword
  argument is `aws_service`, not `service_name`** (and it also wants
  `aws_region` explicitly rather than relying on an ambient default) —
  confirmed by reading the installed package's source after a live
  `TypeError`, not by guessing again.
- **A Gateway MCP session opened once at agent cold-start does not survive
  to later invocations.** AgentCore Runtime's `BedrockAgentCoreApp` invokes
  the handler in a fresh AnyIO worker thread each time with no guaranteed
  event-loop affinity across invocations, and anyio's
  `MemoryObjectSendStream`/`ReceiveStream` are bound to the event loop that
  created them. A module-level cached session (opened via
  `asyncio.get_event_loop()` at import time) crashed on the very next
  invocation with `RuntimeError: There is no current event loop in thread
  'AnyIO worker thread'`. Fixed by opening a fresh Gateway MCP session
  per invocation (`asyncio.run()` end-to-end inside the entrypoint) —
  costs one extra `list_tools()` round trip per turn, worth it for
  correctness.
- **Updating `AWS::BedrockAgentCore::Runtime`'s code does NOT move a
  `RuntimeEndpoint`'s live traffic to the new version.** Confirmed live:
  after a code-only Runtime update, `get-agent-runtime` showed the new
  version READY, but `get-agent-runtime-endpoint` still showed the OLD
  version as `liveVersion`, with `targetVersion: null` — because
  CloudFormation saw no property diff on the (unrelated-looking)
  `RuntimeEndpoint` resource and simply didn't touch it. Same bug *class*
  as the `UpdateAgentRuntime`/`UpdateAgentRuntimeEndpoint` race documented
  in a prior class's ModelSelector feature, but this is the plain
  redeploy case, not a concurrent-update race. Fixed by explicitly binding
  `CfnRuntimeEndpoint(agent_runtime_version=self.runtime.attr_agent_runtime_version)`
  so a Runtime version bump forces a real endpoint property diff (and
  giving the runtime-waiter custom resource a varying property too, so it
  re-polls for READY on every version bump, not just on first create).
- **Cross-region inference profiles route `Converse` calls to foundation
  models in *other* regions than the caller's own.** This account/stack
  runs in us-east-2, but a live `AccessDeniedException` named a us-east-1
  foundation-model resource for the exact same `us.anthropic.claude-sonnet-4-5...`
  inference-profile ID already confirmed working in us-east-2 earlier in
  this same session — the IAM grant needs `arn:aws:bedrock:*::foundation-model/*`
  (region wildcarded; these ARNs carry no account ID so this is safe), not
  `arn:aws:bedrock:<this-region>::foundation-model/*`.
- **AgentCore Gateway passes a Lambda target's tool name via the Lambda
  *client context* (`context.client_context.custom["bedrockAgentCoreToolName"]`),
  not in the event body.** Every real tool call returned `"Unknown tool
  ''"` until this was found — `event.get("bedrockAgentCoreToolName")` is
  always absent from the event itself. Confirmed by a scratch debug branch
  that dumped both `event` and `context.client_context` on an unknown-tool
  response, then removed once the fix was verified live.
- **stdout is fully block-buffered, not line-buffered, inside the
  AgentCore Runtime container.** A bare `print()` used to debug the tool-name
  bug above never appeared in CloudWatch Logs even several minutes later
  and even though the invocation's own structured "Invocation completed"
  log line appeared immediately — only `print(..., flush=True)` (and
  `sys.stderr.flush()` after `traceback.print_exc()`) actually shows up.
  Worth remembering for any future debugging in this runtime: an
  unflushed print is not "delayed," it may never appear until the
  container happens to be reused or recycled.
- **`trace.guardrail` is present on every single Guardrail-enabled
  `Converse` response, whether or not anything happened** — a fully benign
  message still returns a complete trace tree with
  `actionReason: "No action."`. The only reliable live signal that the
  Guardrail actually intervened is the API's own top-level
  `stopReason == "guardrail_intervened"`. An initial `if resp.get("trace",
  {}).get("guardrail")` check was always true, so `guardrail_intervened`
  was `True` on every single turn (including RAG-only questions with zero
  PII) until this was fixed.
- **The system prompt can pre-empt the Guardrail from ever having anything
  to mask** — confirmed live, not a bug: asking "My SSN is
  123-45-6789, can you confirm..." makes the model *itself* decline to
  process the SSN (per an explicit system-prompt rule) and it never echoes
  the digits back, so the Guardrail's sensitive-info policy has nothing to
  act on and genuinely reports `actionReason: "No action."` /
  `guardrail_intervened: false`. This is two independent, valid defense
  layers, not a broken one — `smoke_test.py` demonstrates genuine masking
  instead with PII the system prompt doesn't specifically call out (an
  email + phone number), which reliably triggers
  `stopReason: "guardrail_intervened"` and `{EMAIL}`/`{PHONE}` masking in
  the model's response. Don't "fix" the SSN case by weakening the system
  prompt's own PII-avoidance instruction.

## Repository layout

```
app.py                      CDK app entrypoint (two-phase: agent code must
                             exist in S3 before AgentRuntime/Api stacks synth)
policy_portal/               CDK stacks
lambdas/                     presignup, tools (Gateway target), proxy,
                             kb_ingest (ingestion trigger), runtime_waiter
agents/policy_agent/         the AgentCore Runtime entrypoint
knowledge/corpus/            5 synthetic policy documents
frontend/                    React SPA
scripts/                     package_agent.sh, deploy_frontend.sh,
                             seed_leave_balances.py, smoke_test.py
tests/                       moto-mocked unit tests
```
