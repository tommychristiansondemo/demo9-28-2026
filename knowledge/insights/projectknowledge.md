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

**Full architecture, 9 CDK stacks (`PolicyPortal-*` prefix, 8 in us-east-2 +
1 cert stack in us-east-1):**

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

**Conversation history**: handled client-side, not server-side. The React
frontend keeps the full message list in browser state and resends it as a
`history` array with every request; the backend (both the proxy Lambda and
the agent) is stateless between turns and simply rebuilds its working message
list from whatever `history` the browser sends. There is a `session_id`
(padded to AgentCore's required 33+ character minimum), but it is only used
to correlate the AgentCore Runtime invocation and its logs — it does not
retrieve any stored server-side conversation state. A lost or refreshed
browser tab loses the conversation entirely. This is a known, disclosed
limitation, not a bug — a production system handling weeks/months-long
conversations would need a server-side session store and likely AgentCore's
own Memory feature (short-term + long-term, with automatic summarization),
rather than resending raw history forever.

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
