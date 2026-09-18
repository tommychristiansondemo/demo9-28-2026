# Class Insights knowledge base

This document is read directly into the Class Insights chatbot's system prompt.
It is the single source of truth the chatbot uses to answer student questions
about how this class's demo applications were designed and deployed. Keep it
in sync: any time either application's design changes, update this file and
redeploy `PolicyPortal-InsightsSite` (a `BucketDeployment` syncs it into the
private knowledge bucket on every deploy of that stack).

Account `154833006816`, region **us-east-2** for everything except the
CloudFront/ACM certificate for the main app, which is in **us-east-1** (a
hard CloudFront requirement, not a design choice). Route 53 zone
`awsteach.com`.

## Application 1: Policy & Benefits Assistant (`www.awsteach.com`)

**What it is.** A single, unified, secure portal for a mixed government/
corporate/education workforce. Employees ask questions about organizational
policy (leave, benefits, code of conduct, remote work, data handling) grounded
in a real document corpus, check their own leave balance, and submit a formal
leave/benefits request — all through one chat interface.

**Why it exists.** Built for the one-day class "Building Agentic AI with
Amazon Bedrock AgentCore," to demonstrate three enterprise-grade Bedrock
capabilities together in one realistic app: Retrieval-Augmented Generation,
an "Action Groups"-style tool-calling capability, and Guardrails.

**Full architecture, 10 CDK stacks (`PolicyPortal-*` prefix, 9 in us-east-2 +
1 cert stack in us-east-1) for the core app** — plus 3 more optional/
feature stacks described later in this document (InsightsSite, InsightsApi,
ModelSelector):

- **Network** — a VPC with no NAT gateway and no internet gateway
  (`10.90.0.0/16`, isolated subnets only). All AWS service access goes over
  PrivateLink interface endpoints (bedrock-runtime, bedrock-agent-runtime,
  bedrock-agentcore, bedrock-agentcore-gateway, logs, sts, cognito-idp) or the
  S3/DynamoDB gateway endpoints. No Lambda or the Runtime itself can reach the
  public internet directly — everything is service-to-service over AWS's
  private network, consistent with a compliance-minded audience.
- **Auth** — a Cognito User Pool: self-signup, username-only (no email
  required), 6-character minimum password with no complexity rules, and a
  PreSignUp Lambda that auto-confirms every signup immediately (zero-friction
  onboarding for a one-day class — students never wait on an email
  confirmation link).
- **Data** — two DynamoDB tables (`LeaveBalanceTable`, `RequestsTable`, the
  latter with a `username`-keyed GSI) backing the mock secure API, plus two S3
  buckets: one holding the synthetic policy corpus (5 Markdown documents —
  leave policy, code of conduct, benefits handbook, remote-work policy,
  data-handling/compliance policy, all explicitly synthetic, no real company
  data), and one versioned bucket holding the AgentCore Runtime's deployable
  code artifact.
- **Guardrail** — a real `AWS::Bedrock::Guardrail`: PII entity anonymization
  on 8 entity types (US Social Security Number, email, phone, name, address,
  credit/debit card number, US bank account number, age) plus standard content
  filters (hate, insults, sexual, violence, misconduct, prompt-attack) at
  MEDIUM strength. Wired into **every** `Converse` call the agent makes via
  `guardrailConfig`, both on the way in and the way out. This is the one
  enterprise feature none of the three prior class demos in this series used.
- **KnowledgeBase** — a Bedrock Knowledge Base for RAG, backed by **S3
  Vectors** (not OpenSearch Serverless — no idle-OCU-floor cost, appropriate
  for a one-day class), embedding model `amazon.titan-embed-text-v2:0`. A
  custom resource triggers (and waits for) ingestion on every deploy so the
  corpus is searchable immediately.
- **Gateway** — an AgentCore Gateway (MCP protocol, `AWS_IAM` authorizer)
  fronting a Lambda that implements two tools: `check_leave_balance` and
  `submit_request`. This is the **"Action Groups" equivalent** for this app —
  classic Bedrock Agents has hit "Maintenance Mode, new agent creation not
  available" in this AWS account on prior attempts, so this app deliberately
  builds the same "agent securely calls a backend API" capability on
  AgentCore Gateway + Runtime instead, which also fits the class's explicit
  AgentCore focus better.
- **AgentRuntime** — the agent itself runs on **AgentCore Runtime**, deployed
  via direct-code S3 zip upload (no Docker/ECR container needed). The agent
  code (`agents/policy_agent/agent.py`) is a **manual `boto3` `Converse`
  tool-use loop**, deliberately not the Strands SDK, specifically so the
  Guardrail parameters are guaranteed to be present on every single model
  call rather than depending on framework-level guardrail support. It
  connects to the Gateway via `mcp_proxy_for_aws`'s
  `aws_iam_streamablehttp_client`, opening a **fresh MCP session on every
  invocation** (a session cached at cold start does not survive to later
  invocations — AgentCore Runtime invokes each call in a fresh AnyIO worker
  thread with no event-loop affinity guarantee).
- **Api** — a **Lambda Function URL**, not API Gateway. A live multi-tool
  agentic turn can exceed API Gateway's hard, non-configurable 29-second
  integration timeout; Lambda Function URLs have no such limit. The proxy
  Lambda's only job is `bedrock-agentcore:InvokeAgentRuntime`; it validates
  the caller's identity via `cognito-idp:GetUser` on the Cognito **access
  token** (not the ID token) sent as the Bearer header.
- **Frontend** — a React SPA in S3 behind CloudFront (Origin Access Control,
  not a public bucket), ACM certificate in us-east-1, Route 53 alias at
  `www.awsteach.com`. Reads a runtime `/config.json` written from live stack
  outputs, so a backend-only redeploy never needs a frontend rebuild.
- **Memory** — a `AWS::BedrockAgentCore::Memory` resource backing real
  server-side conversation history (see the dedicated section below). Fully
  independent stack, imported one-directionally by AgentRuntime and Api.

**Conversation history**: handled **server-side**, by a real **Bedrock
AgentCore Memory** resource (`PolicyPortal-Memory` stack) — not client-resent
anymore. The browser only remembers which `session_id` is its own
(`localStorage`, scoped per signed-in username); it no longer sends or stores
any conversation content itself. Concretely:
- **Short-term memory**: on every turn, the agent calls `list_events` for
  `(actorId=username, sessionId)` to reconstruct the session's recent turns
  before calling the model, then calls `create_event` after replying to
  persist that turn (both sides, one atomic call). A page reload genuinely no
  longer loses the conversation — confirmed live: a fresh request with no
  client-sent history, reusing the same `session_id`, correctly recalled a
  fact from an earlier turn in that session.
- **Session isolation**: a different `session_id` for the same user does
  **not** see another session's short-term events — confirmed live.
- **Long-term memory**: a managed **Summarization** strategy extracts a
  running summary per session, asynchronously. AWS requires `{sessionId}` to
  be part of a Summarization strategy's namespace template (confirmed live —
  a purely per-actor namespace with no `{sessionId}` is rejected at
  `CreateMemory` with a validation error); cross-session recall is achieved
  at **retrieval** time instead, by searching the
  `/policyportal/{actorId}/summary/` namespace **prefix**, which matches
  every session's child namespace for that actor via `retrieve_memory_records`
  (that call's own IAM permission and plumbing were verified live — it
  executes cleanly and returns results with no error — but no extracted
  long-term content had actually landed yet within this build's verification
  window; extraction is asynchronous and its real-world latency was not
  observed, so don't claim a specific delay).
- The read path is also exposed directly: `GET <apiUrl>?session_id=...`
  (Cognito-authenticated) lets the frontend redraw history on page load
  without invoking the model at all — a cheap `list_events` call, not a full
  agent turn.
- Only the human-readable exchange (the user's message, the assistant's
  reply) is persisted as memory content — intermediate tool-call JSON from
  the `Converse` tool-use loop is deliberately not stored as "conversation,"
  keeping memory readable. That technical detail is exactly what the
  separate reasoning log (below) is for instead.
- This replaced the app's original design, where the browser resent the
  entire message list as a `history` array on every request and the backend
  was fully stateless between turns — that didn't scale to long conversations
  (linear token/cost growth per turn, a fixed context-window ceiling, and a
  lost/refreshed tab losing everything). AgentCore Memory is the production
  fix for exactly that gap.

**Reasoning/audit logging**: the agent logs one structured JSON line per
`Converse` call (timestamp, session ID, username, model ID, the triggering
user text, which tools were called, the model's stop reason, whether the
Guardrail intervened, and a truncated copy of the response text) to its own
stdout, which AgentCore Runtime routes to CloudWatch Logs. **The real log
group for this content is the one with the `-prod` suffix**
(`/aws/bedrock-agentcore/runtimes/policy_portal_agent-<id>-prod`), not the
`-DEFAULT`-suffixed one AgentCore's own documentation describes for the
default endpoint — confirmed by checking actual log content, not naming
assumptions. This reasoning log is exactly what the `search_logs` tool in
*this* Insights chatbot is allowed to query.

**What's real vs. simulated, stated plainly:**
- The policy corpus (5 documents) is entirely synthetic, written for this
  class, explicitly marked so in each file — not a real organization's actual
  policies.
- The leave-balance and request data in DynamoDB is seeded, synthetic test
  data, not real HR records.
- The Guardrail is fully real and live — PII masking and content filtering
  actually execute on Bedrock's infrastructure, not mocked in application
  code. The reliable signal that it actually did something on a given turn is
  the API's own `stopReason == "guardrail_intervened"` — `trace.guardrail` is
  present on every response regardless of outcome (even a benign message
  returns a full trace tree with `actionReason: "No action."`), so eyeballing
  the trace alone is not sufficient evidence.
- There is **no dedicated CloudTrail/audit stack** in this app (unlike an
  earlier class's Evernorth demo, which had one). The audit/observability
  story here is the structured reasoning log described above plus AgentCore
  Runtime's own built-in unified observability, not a separate CloudTrail
  trail — don't claim CloudTrail coverage for this app that doesn't exist.

### Feature: Model Tuning ("Bedrock flexibility" demo)

**What it is.** A live demonstration, visible on the main app's page below
the chat (no sign-in required — same no-auth design as the Insights app,
since it carries no student data), that lets anyone adjust three sliders
(Quality / Cost / Latency, always summing to 100%) and click "Evaluate &
Apply" to score 5 candidate LLMs against those preferences. If a different
model wins under the current weights, the live AgentCore Runtime is actually
redirected to it — not simulated.

**The 5 candidates**, a real spread across 2 vendors: Claude Opus 4.5
(highest illustrative quality, highest illustrative cost), Claude Sonnet 4.5
(the app's original default, balanced), Claude Haiku 4.5, Amazon Nova Pro,
Amazon Nova Micro (cheapest/fastest illustrative profile).

**What's real vs. illustrative, stated plainly:** `quality_score` and
`cost_score` for each candidate are static, illustrative, documented
reference values in `lambdas/model_selector/handler.py` — there's no fast
way to genuinely benchmark "quality" in a button click. `latency_score` is
the one dimension actually measured live, fresh, on every "Evaluate & Apply"
click: 5 concurrent real `Converse` calls, timed, fastest scores 100. A
consequence of this, confirmed live and working as designed (not a bug): at
the default 50/25/25 weights the ranking does not reliably keep Sonnet 4.5
on top, since cost and latency carry enough combined weight to occasionally
favor a cheaper/faster model — this demonstrates that "best model" genuinely
depends on what you weight, it isn't a fixed answer.

**Where the weights actually live.** Saved weights are genuinely written to
**AppConfig** (Application/Environment/ConfigurationProfile, a custom
zero-final-bake-time Deployment Strategy) on every "Evaluate & Apply" click
— this is the literal "parameters stored in AppConfig" requirement, not just
an in-memory value. A small DynamoDB table caches the latest weights/ranking/
current-model so the page can render instantly without an AppConfig read
round trip.

**How the live switch actually works.** This is the one part of the app
that runs on **AgentCore Runtime**, so switching its model is a two-phase,
synchronous flow inside the same Lambda invocation (the Function URL allows
up to 15 minutes, unlike API Gateway's hard 29-second ceiling): call
`UpdateAgentRuntime` with a new `MODEL_ID` environment variable, poll
`GetAgentRuntime` until the new version reaches `READY` (typically well
under 2 minutes), then call `UpdateAgentRuntimeEndpoint` to actually move
live traffic, and poll that until it settles too. Firing both update calls
back-to-back without waiting throws `ConflictException` — this polling
sequence exists specifically to avoid that.

**Architecture: one more independent stack, `PolicyPortal-ModelSelector`.**
Deliberately has **no inbound dependency** from `PolicyPortal-Api` or
`PolicyPortal-AgentRuntime` (only imports the Runtime's id/ARN/role one
directionally) and owns its own Lambda Function URL rather than adding
routes to the main app's existing one — this avoids the CDK
dependency-cycle bug class hit twice already in this series. It is also
**deliberately not placed in the main VPC**: its Bedrock control-plane calls
(`UpdateAgentRuntime` etc.) have no CDK-exposed PrivateLink endpoint
constant, and Lambdas outside the customer VPC reach AWS's public endpoints
directly with no NAT/endpoint needed — the same precedent already
established by `lambdas/runtime_waiter`. This is a narrow, deliberate
exception to the rest of the app's VPC-isolation posture, made specifically
because this Lambda is an admin/control action, not one that touches
student data.

## Application 2: Class Insights Assistant (this app)

**What it is.** A second, completely separate, public application (no login)
so students can ask a chatbot about the design and deployment of both
applications built for this class — including this Insights app itself —
and pull real, live log data on request, without needing AWS console access
or exposing any credentials.

**Why it's separate, and why now.** Tommy (the instructor) deliberately does
not have this kind of "lift the hood" companion built by default for every
class in this series — it's requested explicitly, by name, when he wants it,
specifically so students don't come away thinking Claude Code always deploys
a self-documenting meta-app as standard practice. For this class, he asked
for it directly after the main app was already live.

**Architecture, 2 CDK stacks (`PolicyPortal-InsightsSite`,
`PolicyPortal-InsightsApi`), both us-east-2, reusing the main app's existing
VPC/network rather than provisioning a second one:**

- **InsightsSite** — two S3 buckets: a public-via-CloudFront-OAC static site
  bucket (this chat UI — plain HTML/CSS/vanilla JS, no build step, visually
  distinct accent color from the main app so it's obviously a different
  tool), and a **private** knowledge bucket holding `projectknowledge.md`
  (this very file), readable only by the Insights chat Lambda's own IAM role
  — never public, never served over CloudFront. Served at its own CloudFront
  distribution's default `*.cloudfront.net` domain — deliberately **no**
  custom subdomain or ACM certificate, to skip an unnecessary DNS-validation
  wait for what is an internal teaching tool, not the primary public app.
- **InsightsApi** — a **Lambda Function URL** (same reasoning as the main
  app's API: no 29-second ceiling), running a manual `boto3` `Converse`
  tool-use loop. System prompt = the live contents of `projectknowledge.md`,
  read from S3 and cached in-Lambda for about 60 seconds so a
  `projectknowledge.md` update propagates without needing a Lambda redeploy.
  One tool, `search_logs`, restricted via IAM to an **explicit allow-list**
  of specific CloudWatch log group ARNs (not a wildcard — a real security
  boundary): the main app's `-prod` reasoning-log group, its API proxy
  Lambda's log group, its Gateway tools Lambda's log group, and this
  Insights Lambda's own log group.
- **No API keys anywhere.** The frontend calls only this app's own Function
  URL; all AWS access (Bedrock `Converse`, S3 read of the knowledge bucket,
  CloudWatch Logs queries) happens server-side through the Lambda's IAM
  execution role. Nothing resembling a secret or API key is ever shipped to
  the browser.
- **No authentication.** Anyone with the URL can chat with it — deliberate,
  since its whole purpose is transparency for every student, and it carries
  no transactional capability (read-only: answers questions and reads logs,
  never writes anything).
- Linked from the main app's front page as a "Class Insights" button/link, so
  students can get there without needing to be told the separate URL.

**Standing requirement — keep this current.** Any future change to either
application's design or deployed resources requires updating this file and
redeploying `PolicyPortal-InsightsSite` so the change is reflected here. This
is not a one-time snapshot.
