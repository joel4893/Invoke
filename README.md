# Invoke

Real-world execution for AI agents.

Invoke sits between AI agents and production tools so agent actions do not become wrong CRM updates, duplicate charges, stale approvals, or impossible debugging sessions.

Agents can reason. Production breaks when they execute.

Invoke turns every tool call into a controlled execution:

- validate schema, scope, and policy
- block wrong-entity actions before they touch the tool
- retry safe failures without custom glue
- reconcile unknown outcomes before retrying
- prevent duplicate side effects with idempotency
- freeze risky work for approval, then revalidate before execution
- trace what happened end to end

## Quick Start

```bash
# 1. Install the CLI
npm install -g @invokehq/cli

# 2. Authenticate to your Invoke runtime
invoke login --base-url https://api.invokehq.run --api-key inv_live_...

# 3. Scaffold an execution project
invoke init support-agent --template crm-guardrail
cd support-agent

# 4. Run its local MCP server
invoke dev install

# 5. Register its tools with Invoke
invoke deploy

# 6. Call a tool through the execution layer
invoke call crm_update_customer '{"customer_id":"cust_123","account_status":"review"}'
```

Then call tools through one execution layer:

```ts
import { Invoke } from "./sdk";

const invoke = Invoke.fromEnv();

await invoke.call({
  tool: "linear.create_issue",
  params: {
    team_id: "team_123",
    title: "Investigate webhook drift",
  },
  agentId: "prod-support-agent",
  idempotencyKey: "linear:Investigate webhook drift:team_123",
});
```

That's it. Your agent still chooses the action. Invoke makes the execution reliable: scoped, checked, retried, reconciled, approved, and traced.

## What You Can Build

Invoke ships with wrapper generation for common production tools:

| Command | What it creates |
| --- | --- |
| `invoke wrap github` | GitHub MCP wrapper with approval-ready issue creation metadata |
| `invoke wrap linear` | Linear issue workflow wrapper with idempotency hints |
| `invoke wrap notion` | Notion page/document wrapper with Invoke capability metadata |
| `invoke wrap postgresql --query "SELECT ..."` | Scoped PostgreSQL query tool with inferred input schema |
| `invoke wrap billing-api --openapi openapi.json --base-url https://billing.example.com` | OpenAPI-backed wrapper for an internal service |

Or start from an existing MCP server and register its capability card with Invoke:

```bash
invoke init billing-agent --template default
cd billing-agent
# run a local MCP endpoint, or edit invoke.json to point at your hosted MCP URL
invoke dev
invoke deploy
```

## See The Aha Moment

Run the local failure demo:

```bash
./demo/run_demo.sh
```

It starts mock tools and shows six production failure modes:

- tool timeout recovered with bounded retry
- payment timeout reconciled before a duplicate charge
- wrong CRM update blocked before the record is touched
- duplicate retry replayed instead of creating a second issue
- stale approval requeued after live state changed
- webhook inconsistency returned as `replan_required`

The buyer takeaway is simple:

```text
Invoke does not make agents smarter.
It makes their execution bounded when production gets messy.
```

## How It Works

```text
Agents call tools       Invoke controls execution       Production systems
+----------------+      +------------------------+      +------------------+
| SDK / HTTP     |      | Scope + schema check   |      | Linear           |
| MCP clients    |----->| Retry + reconciliation |----->| Slack            |
| workflows      |      | Approval + revalidate  |      | CRM / billing    |
| background jobs|<-----| Trace + outcome        |<-----| Internal APIs    |
+----------------+      +------------------------+      +------------------+
```

Define - Wrap a service with `invoke wrap` or register an existing MCP tool with a capability card, schema, risk level, and retry/idempotency hints.

Execute - Agents call `/call` through the SDK or HTTP. Invoke validates the request, checks scope, classifies the action, and routes it to the right tool.

Control - If the tool times out, partially succeeds, or returns an unknown outcome, Invoke reconciles current state before retrying. If the action is risky, Invoke freezes it for approval and revalidates live state before execution.

Trace - Every call gets a structured execution record your team can inspect, export, and debug.

## SDK And API

### 1. Get an API key

Ask for an Invoke API key, then export it in your shell:

```bash
export INVOKE_API_KEY="inv_or_ag_live_..."
export INVOKE_BASE_URL="https://api.invokehq.run"
```

Every API request uses:

```text
X-API-Key: $INVOKE_API_KEY
```

### 2. Check available tools

This confirms your key works and shows the tools Invoke can route to.

```bash
curl "$INVOKE_BASE_URL/v1/tools" \
  -H "X-API-Key: $INVOKE_API_KEY"
```

### 3. Retrieve live context with Exa

This is what we mean by `curl /v1/search`: your agent asks Invoke for fresh docs or web context before acting. Invoke calls Exa server-side and returns normalized sources plus a trace.

```bash
curl -X POST "$INVOKE_BASE_URL/v1/search" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $INVOKE_API_KEY" \
  -d '{
    "query": "latest MCP agent failures",
    "limit": 3
  }'
```

### 4. Create a safe execution

This is what we mean by `curl /v1/executions`: your agent asks Invoke to run a workflow with an idempotency key. If the request is repeated after a timeout, Invoke replays the completed execution instead of creating duplicate side effects.

```bash
curl -X POST "$INVOKE_BASE_URL/v1/executions" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $INVOKE_API_KEY" \
  -H "Idempotency-Key: demo-charge-001" \
  -d '{
    "workflow": "safe-tool-execution",
    "agent_id": "revops_agent",
    "input": {
      "params": {
        "customer_id": "cust_acme",
        "amount": 2400,
        "currency": "usd"
      }
    }
  }'
```

The response includes a durable execution object:

```json
{
  "success": true,
  "execution": {
    "execution_id": "exec_142",
    "workflow_id": "safe-tool-execution",
    "status": "completed",
    "idempotency_key": "demo-charge-001",
    "final_outcome": "completed",
    "trace": [
      {"step": "request_received", "status": "completed"},
      {"step": "unknown_outcome_reconciled", "status": "completed"},
      {"step": "duplicate_retry_blocked", "status": "completed"}
    ]
  }
}
```

### 5. Add company context

Use the company brain when an agent needs canonical enterprise context before it acts. Ingest CRM, ERP, docs, or legacy records as entities, facts, and relationships:

```bash
curl -X POST "$INVOKE_BASE_URL/v1/brain/ingest" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $INVOKE_API_KEY" \
  -d '{
    "source": "crm",
    "confidence": 0.99,
    "entities": [
      {"type": "customer", "id": "cust_123", "name": "Acme Corp", "aliases": ["ACME"]},
      {"type": "invoice", "id": "inv_777", "properties": {"status": "overdue"}}
    ],
    "facts": [
      {"entity_id": "cust_123", "key": "arr", "value": 250000}
    ],
    "edges": [
      {"from": "cust_123", "relation": "has_invoice", "to": "inv_777"}
    ]
  }'
```

Then query the graph directly or require it during execution:

```bash
curl -X POST "$INVOKE_BASE_URL/v1/brain/query" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $INVOKE_API_KEY" \
  -d '{"query":"ACME","entity_type":"customer","join_to":{"entity_type":"invoice"}}'
```

```json
{
  "tool": "crm.update_customer",
  "params": {"customer_id": "ACME", "status": "review"},
  "company_brain": {"entity_id": "cust_123", "entity_type": "customer", "required": true}
}
```

Invoke resolves aliases through the graph and blocks the call if required company context cannot be verified.

### 6. Simulate policy and monitor outcomes

Use `/v1/policy/simulate` for pre-execution checks without touching production:

```bash
curl -X POST "$INVOKE_BASE_URL/v1/policy/simulate" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $INVOKE_API_KEY" \
  -d '{"agent_id":"finance_agent","action":"database.execute","params":{"sql":"drop table invoices"}}'
```

Use `/v1/observability/summary` for agent health, guardrail ROI, failure patterns, and company-brain data quality. Use `/v1/governance/readiness` for policy-as-code, auditability, human oversight, and EU AI Act readiness checks.

### 7. Use Invoke as an MCP server

Invoke exposes its own MCP endpoint at `/v1/mcp`. This lets agents inspect the execution layer itself through MCP before they touch production tools.

```bash
curl -X POST "$INVOKE_BASE_URL/v1/mcp" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $INVOKE_API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

Useful Invoke-native MCP tools include:

- `invoke.company_brain.query`
- `invoke.company_brain.ingest`
- `invoke.policy.simulate`
- `invoke.preflight`
- `invoke.executions.list`
- `invoke.approvals.list`
- `invoke.observability.summary`
- `invoke.governance.readiness`

Start MCP clients with a scoped key, not a full production key:

```bash
curl https://api.invokehq.run/v1/mcp/key-profiles

curl -X POST "$INVOKE_BASE_URL/v1/beta/users/$USER_ID/keys" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $INVOKE_ADMIN_KEY" \
  -d '{"name":"Claude desktop MCP","profile":"mcp_readonly"}'
```

Smoke-test the live MCP surface without mutating production:

```bash
export INVOKE_BASE_URL="https://api.invokehq.run"
export INVOKE_API_KEY="inv_live_..."
venv/bin/python scripts/test_mcp_smoke.py
```

Example MCP tool call:

```bash
curl -X POST "$INVOKE_BASE_URL/v1/mcp" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $INVOKE_API_KEY" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
      "name": "invoke.policy.simulate",
      "arguments": {
        "agent_id": "finance_agent",
        "action": "database.execute",
        "params": {"sql": "drop table invoices"}
      }
    }
  }'
```

### 8. Use the SDK

Python:

```python
from sdk import Invoke

invoke = Invoke.from_env()

context = invoke.search("latest MCP agent failures", limit=3)
print(context["results"][0]["url"])

execution = invoke.execute(
    workflow="safe-tool-execution",
    agent_id="revops_agent",
    idempotency_key="demo-charge-001",
    input={
        "params": {
            "customer_id": "cust_acme",
            "amount": 2400,
            "currency": "usd",
        }
    },
)

print(execution["execution"]["execution_id"])
print(execution["execution"]["final_outcome"])
```

TypeScript:

```ts
import { Invoke } from "./sdk";

const invoke = Invoke.fromEnv();

const context = await invoke.search("latest MCP agent failures", { limit: 3 });
console.log(context.results);

const execution = await invoke.execute({
  workflow: "safe-tool-execution",
  agentId: "revops_agent",
  idempotencyKey: "demo-charge-001",
  input: {
    params: {
      customer_id: "cust_acme",
      amount: 2400,
      currency: "usd",
    },
  },
});

console.log(execution.execution);
```

### 6. Run packaged workflows

The CLI wraps the same API:

```bash
invoke search "latest MCP agent failures"
invoke workflow safe-tool-execution
invoke workflow live-context-retrieval --query "latest OpenAI MCP auth changes before deploying"
invoke workflow failure-trace-visualization
```

The workflow response includes a buyer-readable `visual_flow` and a structured `trace`, for example:

```text
request_received -> context_retrieved -> risk_scanned -> tool_authorized -> execution_completed
```

### 7. Call a tool directly

```bash
curl -X POST "$INVOKE_BASE_URL/v1/call" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $INVOKE_API_KEY" \
  -d '{
    "tool": "fetch.url",
    "params": {"url": "https://github.com"},
    "agent_id": "research_agent_v1"
  }'
```

## Deploy on Render

This repo is ready to deploy to Render as a Docker web service.

### 1. Push this repo to GitHub

Render deploys from your Git branch, so make sure the latest backend code is pushed first.

### 2. Create the service from `render.yaml`

In Render, create a new Blueprint and point it at this repo. The checked-in [render.yaml](render.yaml) provisions:

- a Docker web service named `invoke-api`
- a health check at `/health`
- env vars for Invoke plus Supabase-backed persistence

### 3. Fill the required secrets

Render will prompt for:

- `INVOKE_API_KEYS`: comma-separated API keys allowed to call Invoke
- `INVOKE_PUBLIC_URL`: your deployed URL, such as `https://invoke-api.onrender.com`
- `INVOKE_ALLOWED_ORIGINS`: comma-separated frontend origins allowed to call the API, such as `https://invoke.vercel.app`
- `INVOKE_ALLOWED_ORIGIN_REGEX`: optional regex for preview deployments, such as `https://.*\\.vercel\\.app`
- `SUPABASE_URL`: your Supabase project URL, for example `https://xyzcompany.supabase.co` (the backend also tolerates a full `/rest/v1` URL if you already copied that)
- `SUPABASE_SERVICE_ROLE_KEY`: your Supabase service role key
- `LINEAR_API_KEY`: Linear API key for real issue creation
- `LINEAR_TEAM_ID`: optional default Linear team UUID for hosted demos
- `SLACK_BOT_TOKEN`: Slack bot token for real channel listing and message posting
- `SLACK_DEFAULT_CHANNEL`: optional default Slack channel ID for hosted demos
- `EXA_API_KEY`: Exa API key for `/search` and `invoke search`

### 4. Create the Supabase tables

Open the Supabase SQL editor and run [supabase/schema.sql](supabase/schema.sql).

That creates the tables Invoke uses for:

- connected providers
- provider keys
- dynamic tools
- pending approvals
- tool-call traces
- execution records

If Render logs `supabase_schema_missing` or a PostgREST `404` for
`invoke_providers`, the API is alive but persistence is not installed yet. Run
the schema file above in the same Supabase project used by `SUPABASE_URL`.

### 5. Verify the deployment

Health check:

```bash
curl https://YOUR-SERVICE.onrender.com/health
```

Tool registry:

```bash
curl https://YOUR-SERVICE.onrender.com/tools \
  -H "X-API-Key: YOUR_API_KEY"
```

### Notes

- If `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are set, Invoke stores runtime state in Supabase instead of local SQLite files.
- `TRACE_DB`, `TRACE_LOG_FILE`, and `TRACE_EVENTS_FILE` still exist as local fallbacks for development and emergency startup.
- Keep the Supabase service role key server-side only. Do not expose it in browser code or client SDKs.
- If a Vercel frontend calls the Render backend directly from the browser, set `TRACE_ALLOWED_ORIGINS` and, if you use preview deploys, `TRACE_ALLOWED_ORIGIN_REGEX`.
- For Slack, the bot token needs `chat:write` to send messages and `channels:read` / `groups:read` if you want the frontend to list channels.

### Vercel Frontend -> Render Backend

This repo does not include a Next.js or Vercel frontend yet, but the backend is ready for one.

Frontend env vars:

```bash
NEXT_PUBLIC_INVOKE_BASE_URL=https://invoke-ai.onrender.com
NEXT_PUBLIC_INVOKE_API_KEY=YOUR_PUBLIC_DEMO_KEY
```

Backend env vars on Render:

```bash
INVOKE_PUBLIC_URL=https://invoke-ai.onrender.com
INVOKE_ALLOWED_ORIGINS=https://your-frontend.vercel.app
INVOKE_ALLOWED_ORIGIN_REGEX=https://.*\\.vercel\\.app
```

For server-side calls from a Vercel route handler or server action, use the existing SDK env names instead:

```bash
INVOKE_BASE_URL=https://invoke-ai.onrender.com
INVOKE_API_KEY=YOUR_SERVER_SIDE_KEY
```

## Build A Tool Wrapper

Create launch connector wrappers:

```bash
invoke wrap github
invoke wrap notion
invoke wrap linear
```

Wrap a PostgreSQL query:

```bash
invoke wrap postgresql \
  --query "SELECT * FROM invoices WHERE id = :invoice_id" \
  --name "invoice lookup"
```

Wrap an OpenAPI service:

```bash
invoke wrap billing-api \
  --openapi openapi.json \
  --base-url https://billing.example.com
```

This creates a runnable MCP wrapper under `wrapped_tools/` with:

- capability metadata
- JSON schema validation
- structured JSON-RPC errors
- idempotency hints
- retry hints
- `invoke.register.json` for provider onboarding

The npm command is the recommended path. The underlying Python entrypoint remains available for local development:

```bash
python agentify.py wrap github
```

## Runtime API

### Connect Hosted MCP Gateway

```python
from sdk import invoke

connected = invoke.connect(
    "github",
    owner_email="dev@acme.example",
    approval_email="ops@acme.example",
)

print(connected["gateway_url"])
print(connected["tools"][0]["key"])
```

The gateway returns a hosted endpoint such as `https://github.invoke.dev` plus an MCP URL and preloaded launch-tool metadata.

### TypeScript SDK

```ts
import { Invoke } from "./sdk";

const invoke = new Invoke({ apiKey: process.env.INVOKE_API_KEY! });

const result = await invoke.call({
  tool: "fetch.url",
  params: { url: "https://github.com" },
  agentId: "research_agent_v1",
});

const connected = await invoke.connect({
  saas: "linear",
  ownerEmail: "dev@acme.example",
});
```

### HTTP

All runtime endpoints require `X-API-Key`.

Create and inspect a v1 execution:

```bash
curl -X POST http://localhost:8000/v1/executions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $INVOKE_API_KEY" \
  -H "Idempotency-Key: checkout-retry-001" \
  -d '{"workflow":"safe-tool-execution","input":{"params":{"invoice_id":"inv_123"}}}'

curl http://localhost:8000/v1/executions \
  -H "X-API-Key: $INVOKE_API_KEY"
```

List registered tools:

```bash
curl http://localhost:8000/tools \
  -H "X-API-Key: $INVOKE_API_KEY"
```

Call a tool:

```bash
curl http://localhost:8000/v1/call \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $INVOKE_API_KEY" \
  -d '{
    "tool": "fetch.url",
    "params": {"url": "https://github.com"},
    "agent_id": "research_agent_v1"
  }'
```

Connect a launch SaaS and get its hosted gateway:

```bash
curl -X POST http://localhost:8000/connect/github \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $INVOKE_API_KEY" \
  -d '{"owner_email": "dev@acme.example"}'
```

## State Revalidation Engine

Agents should act on current truth, not stale assumptions. Use `invoke.verify_state(...)` before execution when an action depends on critical business state:

```python
from sdk import invoke

state = invoke.verify_state(
    intent="send_invoice_reminder",
    required_fields=["invoice_status", "balance"],
    assumed_state={"invoice_status": "unpaid", "balance": 125},
    state_refetch={
        "tool": "fetch.url",
        "params": {"url": "https://billing.example.com/invoices/inv_123"},
    },
    conditions={
        "invoice_status": "unpaid",
        "balance": "> 0",
    },
)

if state["decision"] != "execute":
    return "Invoice is no longer unpaid. Abort reminder."
```

Invoke re-fetches current state, compares it with the decision-time assumptions, computes field-level drift, then returns:

- `verified` / `execute`: required fields still match and conditions pass.
- `blocked` / `abort`: state is missing, changed, or no longer satisfies conditions.
- `replan_required` / `replan`: same mismatch path when `on_mismatch="replan"`.

## Entity Resolution Tracking

Agents should act on the correct entity, not a guessed ID. Attach `entity_resolution` to a tool call when the agent resolved a customer, account, invoice, or user before acting:

```python
from sdk import invoke

result = invoke.call(
    tool="billing.send_reminder",
    params={"customer_id": "cust_123", "invoice_id": "inv_456"},
    agent_id="billing_agent",
    entity_resolution={
        "entity_id": "cust_123",
        "source": "crm_lookup",
        "resolved_at": "2026-05-01T12:00:00Z",
    },
)
```

Invoke logs the resolved `entity_id`, source, and timestamp into the tool-call trace. Before execution, it compares that entity against IDs in the action params and execution state. If the action points at a different entity, Invoke blocks the call with `409` and never touches the tool.

The same check runs again when a pending approval is approved. If the thawed state now points at a different customer or account, the approval is blocked instead of executing stale work.

## Failure Policy Engine

Agents should not guess what to do when tools fail. Add a failure policy to a tool call to make retry, fallback, and escalation behavior explicit and bounded:

```json
{
  "retry": 2,
  "fallback": "secondary_api",
  "on_failure": "escalate"
}
```

```python
from sdk import invoke

result = invoke.call(
    tool="billing.primary_lookup",
    params={"invoice_id": "inv_123"},
    agent_id="billing_agent",
    failure_policy={
        "retry": 2,
        "fallback": "billing.secondary_lookup",
        "on_failure": "escalate",
    },
)
```

Invoke enforces a hard retry cap. `retry: 2` means one initial attempt plus two bounded retries, never an infinite loop. If the primary tool still fails, Invoke can call the fallback tool once. If everything fails and `on_failure` is `escalate`, Invoke creates a pending approval with the failure context for a human to review.

## Outcome Reconciliation

Never retry blindly when the outcome is unknown. If a timeout or partial failure happens after a side effect may have occurred, reconcile the action first:

```python
from sdk import invoke

outcome = invoke.reconcile({
    "action": {
        "intent": "charge_customer",
        "params": {"payment_id": "pay_123"},
    },
    "outcome": "UNKNOWN",
    "state_refetch": {
        "tool": "payments.lookup",
        "params": {"payment_id": "pay_123"},
    },
    "conditions": {"charged": True},
})

if outcome["decision"] == "do_not_retry":
    return "Payment already succeeded. Do not charge again."
```

You can also attach reconciliation to a failure policy:

```json
{
  "retry": 2,
  "on_failure": "escalate",
  "reconcile": {
    "action": "charge_customer",
    "state_refetch": {
      "tool": "payments.lookup",
      "params": {"payment_id": "pay_123"}
    },
    "conditions": {"charged": true}
  }
}
```

When a failure has an `UNKNOWN` outcome, Invoke runs reconciliation before retrying. If reconciliation shows the action already succeeded, Invoke returns `outcome_reconciled` and blocks duplicate retries. If reconciliation shows it did not succeed, bounded retry can continue. If the outcome is still unknown, Invoke escalates or errors according to policy.

## Approval Gates

Approval checkpoints carry an execution snapshot: params, variables, tool outputs, action, policy contract, and timestamps. Approval revalidates policy against fresh state before execution and returns `executed`, `cancelled`, `replan_required`, or `requeued`.

### Conditional Approval Contracts

Use a conditional approval contract when an approval is only valid if live business state still matches what the human approved. A contract with `intent` and `conditions` creates a pending approval even if the underlying tool is normally low-risk:

```json
{
  "intent": "send_invoice_reminder",
  "conditions": {
    "invoice_status": "overdue",
    "customer_balance": "> 0"
  },
  "threshold": "strict",
  "expires_at": "2026-04-30T15:00:00Z"
}
```

When approval happens, Invoke fetches or accepts current state, compares it with the frozen approval-time state, and computes drift for each condition key.

- Valid: execute the original action.
- Changed: mark the old approval `invalidated` and create a fresh pending approval with the live state.
- Expired: cancel before execution.

`threshold: "strict"` means condition values must still match the approval-time snapshot exactly and the current values must still satisfy every condition. `threshold: "conditions"` allows value changes as long as the current values still satisfy the conditions.

```python
from sdk import invoke

policy = {
    "rules": [
        {
            "when": "action == git_push and branch == main",
            "effect": "require_approval",
            "intent": "push_to_main",
            "allowed_action": "git_push",
            "reason": "Human approval required for pushes to main",
        }
    ]
}

pending = invoke.call(
    tool="fetch.url",
    params={"url": "https://example.com/repo", "branch": "main"},
    agent_id="dev_agent_v1",
    action="git_push",
    policy=policy,
    execution_state={
        "variables": {"branch": "main"},
        "tool_outputs": {"diff_summary": {"files_changed": 3}},
    },
)
```

A domain policy can freeze intent-specific work:

```json
{
  "intent": "send_invoice_reminder",
  "condition": "invoice_status == overdue",
  "allowed_action": "send_email",
  "expires_at": "2026-04-30T15:00:00Z"
}
```

When the human approves, Invoke can accept fresh state from the approver or run a configured `state_refetch` read tool. It then thaws the checkpoint, validates `condition`, checks `allowed_action` and `expires_at`, and decides whether to execute, cancel, or ask the agent to re-plan.

Open the approval dashboard:

```bash
open "http://localhost:8000/dashboard/approvals?api_key=$INVOKE_API_KEY"
```

For approval notifications, set:

```bash
export APPROVAL_SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
export APPROVAL_EMAIL_WEBHOOK_URL="https://email-webhook.example/send"
```

## CLI Commands

The CLI gives you the first Invoke project lifecycle:

```bash
invoke login --base-url https://api.invokehq.run --api-key inv_live_...
invoke init support-agent --template crm-guardrail
invoke dev
invoke deploy
invoke call <tool> '{"json":"params"}'
invoke agents list
```

Wrapper generation is still available for production tools:

```bash
invoke wrap github
invoke wrap notion
invoke wrap linear
invoke wrap postgresql --query "SELECT * FROM users WHERE id = :user_id"
invoke wrap billing-api --openapi openapi.json --base-url http://localhost:8000
```

Credentials from `invoke login` are stored in `~/.invoke/credentials.json`. `invoke dev` writes the local MCP URL to `.invoke/dev.json`, and `invoke deploy` uses it automatically when `invoke.json` still has the placeholder MCP URL. Set `INVOKE_HOME` to override the global credentials location in CI.

## Configuration

Local server environment variables:

| Variable | Description |
| --- | --- |
| `INVOKE_API_KEYS` | Comma-separated full-access server API keys |
| `TRACE_API_KEYS` | Existing Trace-compatible server API key env var |
| `INVOKE_API_KEY_SCOPES` | JSON scoped-token config for tool allowlists and scopes |
| `TRACE_API_KEY_SCOPES` | Existing Trace-compatible scoped-token env var |
| `INVOKE_LOG_FILE` | Override JSON audit log filename |
| `TRACE_LOG_FILE` | Existing Trace-compatible audit log filename |
| `INVOKE_ALLOWED_ORIGINS` | Comma-separated browser origins allowed by CORS |
| `TRACE_ALLOWED_ORIGINS` | Existing Trace-compatible CORS allowlist env var |
| `INVOKE_ALLOWED_ORIGIN_REGEX` | Optional regex for preview frontend origins such as Vercel previews |
| `TRACE_ALLOWED_ORIGIN_REGEX` | Existing Trace-compatible preview-origin regex env var |
| `SUPABASE_URL` | Optional Supabase project URL for hosted persistence; `/rest/v1` suffix is tolerated |
| `SUPABASE_SERVICE_ROLE_KEY` | Optional Supabase service role key for hosted persistence |
| `SUPABASE_TABLE_PREFIX` | Optional Supabase table prefix, default `invoke_` |
| `LINEAR_API_KEY` | Optional Linear API key for direct issue creation |
| `LINEAR_TEAM_ID` | Optional default Linear team UUID for hosted demos |
| `SLACK_BOT_TOKEN` | Optional Slack bot token for direct channel listing and posting |
| `SLACK_DEFAULT_CHANNEL` | Optional default Slack channel ID for hosted demos |
| `FAILURE_POLICY_MAX_RETRIES` | Hard cap for per-call failure-policy retries, default `5` |
| `HOST` | Server bind host, default `0.0.0.0` |
| `PORT` | Server bind port, default `8000` |

Client environment variables:

| Variable | Description |
| --- | --- |
| `INVOKE_API_KEY` | API key used by the Python and TypeScript SDKs |
| `TRACE_API_KEY` | Existing Trace-compatible client API key env var |
| `INVOKE_BASE_URL` | SDK base URL override |
| `TRACE_BASE_URL` | Existing Trace-compatible SDK base URL override |

Scoped token example:

```bash
export INVOKE_API_KEY_SCOPES='{
  "inv_scoped_fetch_read": {
    "scopes": ["tools:read", "tools:call", "traces:read"],
    "allowed_tools": ["fetch.url"],
    "read_only": true,
    "agent_id": "research_agent_v1",
    "envs": ["dev", "prod"],
    "agents": ["research_agent_v1"],
    "workflows": ["market_research"],
    "resources": ["cust_123", "repo:acme/app"]
  }
}'
```

Supported scopes today: `tools:read`, `tools:call`, `state:verify`, `outcomes:reconcile`, `approvals:read`, `approvals:write`, `logs:read`, `traces:read`, and `providers:admin`.

`allowed_tools` and `read_only` gate tool access. `envs`, `agents`, `workflows`, `allowed_actions`, and `resources` gate execution context, so a token can mean "this agent may call this tool in prod for this workflow and resource" instead of only re-exposing provider OAuth scopes.

## Observability

List recent tool-call traces:

```bash
curl http://localhost:8000/traces \
  -H "X-API-Key: $INVOKE_API_KEY"
```

Export traces:

```bash
curl "http://localhost:8000/traces/export?format=langsmith" \
  -H "X-API-Key: $INVOKE_API_KEY"
```

Local logs:

- JSON audit logs: `logs/trace.log`
- tool-call traces: `logs/tool_calls.jsonl`
- trace export formats: JSON, JSONL, LangSmith-shaped, and Helicone-shaped records

When Supabase persistence is enabled, `/traces` and `/traces/export` read from Supabase instead of the local JSONL file.

## Blast-Radius Demo

There is a demo harness that starts mock tools and runs a production-failure story. It shows how Invoke contains the blast radius when agent execution gets messy:

- tool timeout / transient upstream failure
- partial success with unknown outcome
- wrong CRM update blocked by entity resolution
- duplicated retry
- stale approval
- webhook inconsistency

```bash
FLAKY_FAIL_FIRST_N=2 ./demo/run_demo.sh
```

The demo starts the flaky MCP simulator, a mock tool/CRM MCP server, the Invoke gateway, then runs `demo_comparison.py` and cleans up processes. The buyer takeaway is simple: Invoke does not make agents smarter; it makes their execution bounded when production gets messy.

## What Exists Today

- API-key authentication
- scoped API tokens with tool allowlists and read-only checks
- npm/npx CLI package with `invoke login`, `invoke init`, `invoke deploy`, `invoke call`, `invoke agents list`, and `invoke wrap`
- `invoke wrap` generator for OpenAPI, GitHub, Notion, Linear, and PostgreSQL MCP wrappers
- hosted gateway URL metadata for connected SaaS tools
- agent-readable tool registry
- `/tools` capability cards
- `/discover` capability search
- provider onboarding with registered tools available in discovery and calls
- `/call` reliable tool invocation
- `/state/verify` state revalidation before execution
- entity resolution tracking with pre-execution mismatch blocking
- failure policy engine for bounded retry, fallback, and escalation
- outcome reconciliation to prevent duplicate retries after unknown results
- policy-as-code `pending_approval` responses
- conditional approval contracts with drift-based requeue
- frozen execution checkpoints with variables and tool outputs
- `/approvals` approval queue
- `/dashboard/approvals` web dashboard for human review
- Slack and email-webhook approval notifications
- approve/reject plus thaw-time execute, cancel, or re-plan decisions
- MCP Streamable HTTP support
- direct HTTP fallback for `fetch.url`
- JSON audit logs in `logs/trace.log`
- tool-call traces in `logs/tool_calls.jsonl`
- `/traces/export` for JSON, JSONL, LangSmith-shaped, and Helicone-shaped records

## Direction

Invoke is moving toward the runtime layer for real-world agent capabilities:

- scoped agent identities
- approval gates for risky actions
- provider onboarding and wrapper templates
- richer capability search
- usage metering and policy controls
- dashboard-grade observability
