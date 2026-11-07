# Invoke CLI

Route your AI agents' tool calls through Invoke. Every call is checked first (the agent's identity,
its permissions, your policies, the budget), a retried call isn't run twice, and each action comes
back with a receipt you can look up later.

```bash
npm install -g @invokehq/cli
invoke --version
```

You need Node 18+ and Python 3.9+ (`invoke` is a small Node launcher for a Python CLI).

## Quick start

```bash
# 1. Save your API key. No key yet? https://console.invokehq.run/sign-up
export INVOKE_API_KEY=inv_live_...
invoke login --api-key "$INVOKE_API_KEY"

# 2. Make a workspace for your agents (or pick an existing one: invoke gateway use ws_...)
invoke gateway create my-agents

# 3. Bring the tools your agent uses: any MCP server, with the auth header it expects
invoke gateway connect GitHub https://api.githubcopilot.com/mcp/ \
  --auth-header Authorization --auth-value "Bearer $GITHUB_TOKEN"

# 4. See what your agents can call, and call one yourself
invoke gateway tools
invoke gateway call github_get_me '{}'

# 5. See what happened
invoke gateway receipts
invoke gateway logs
```

MCP servers that only allow OAuth sign-in (Slack, Linear, Notion and others) are connected from the
web console.

## Point your agent at it

`invoke gateway url` prints your workspace's MCP URL. Give it to any MCP client with your key in an
`X-API-Key: <key>` or `Authorization: Bearer <key>` header.

**Claude Code**

```bash
claude mcp add --transport http invoke "$(invoke gateway url)" --header "X-API-Key: $INVOKE_API_KEY"
```

**LangGraph** (`pip install langchain-mcp-adapters`)

```python
import os
from langchain_mcp_adapters.client import MultiServerMCPClient

client = MultiServerMCPClient({"invoke": {
    "url": "https://api.invokehq.run/v1/workspaces/<ws_id>/mcp",   # from `invoke gateway url`
    "transport": "streamable_http",
    "headers": {"Authorization": f"Bearer {os.environ['INVOKE_API_KEY']}"},
}})
tools = await client.get_tools()   # give these to your LangGraph agent
```

**OpenAI Agents SDK** (`pip install openai-agents`)

```python
import os
from agents import Agent
from agents.mcp import MCPServerStreamableHttp

async with MCPServerStreamableHttp(name="invoke", params={
    "url": "https://api.invokehq.run/v1/workspaces/<ws_id>/mcp",   # from `invoke gateway url`
    "headers": {"Authorization": f"Bearer {os.environ['INVOKE_API_KEY']}"},
}) as invoke:
    agent = Agent(name="assistant", mcp_servers=[invoke])
```

Tools are listed with names a model API accepts (`invoke_claim_task`, `github_get_me`).

**Give each agent its own key.** Create agents and their keys in the web console. An agent's key is
its identity: Invoke records who acted from the key, so an agent can't act under another agent's
name, and every receipt names the agent that acted.

## Commands

| Command | What it does |
|---|---|
| `invoke login` | Save your API key |
| `invoke gateway create <name>` | Make a workspace and make it active |
| `invoke gateway use <ws_id>` | Make an existing workspace active |
| `invoke gateway url` | The MCP URL to give your agent |
| `invoke gateway connect <name> <mcp-url>` | Bring a tool server into the workspace |
| `invoke gateway tools` | List the tools your agents can call |
| `invoke gateway call <tool> '<json>'` | Call one tool through Invoke. Add `--idempotency-key` to make a retry safe |
| `invoke gateway receipts [<id>]` | What ran, with signed receipts. `--verify` checks a receipt's signature |
| `invoke gateway logs` | Show or follow the workspace's event log |
| `invoke gateway status` | The workspace at a glance: tools, budget, governed calls |
| `invoke gateway context` / `memory` | The shared context and facts every agent sees |
| `invoke gateway subscribe` / `broadcast` / `inbox` | Agent-to-agent messages |
| `invoke wrap <target>` | Generate an MCP server that wraps a database or an API |
| `invoke status` / `doctor` / `config` | Your login and config, and the API's health |

Run `invoke --help` or `invoke <command> --help` for details.

## Configuration

| Variable | |
|---|---|
| `INVOKE_API_KEY` | Your API key (overrides the saved one) |
| `INVOKE_BASE_URL` | Default `https://api.invokehq.run` |
| `INVOKE_GATEWAY_WORKSPACE` | The workspace to use (overrides the active one) |
| `INVOKE_HOME` | Where the CLI keeps its config. Default `~/.invoke` |
| `INVOKE_TIMEOUT_SECONDS` | Request timeout. Default 90 |
| `INVOKE_PYTHON` | The Python the launcher runs |

## SDKs

To call Invoke from your own code instead of through MCP:

```bash
pip install invokehq          # Python
npm install @invokehq/sdk     # TypeScript
```

## Upgrading from 0.5

0.6 leads with the gateway. Some older commands ran on Invoke's old runtime API, which the hosted
service no longer serves, so they could only fail. They are retired. Each one now prints its
replacement instead of calling the API:

| Retired | Use instead |
|---|---|
| `invoke init` | `invoke gateway create`, then `invoke gateway connect` |
| `invoke run` | point your agent at `invoke gateway url`, or `invoke gateway call` |
| `invoke inspect`, `invoke receipts` | `invoke gateway receipts` |
| `invoke logs` | `invoke gateway logs` |
| `invoke graph` | the agent graph in the web console |
| `invoke replay` | not on the gateway yet. Retry with the same `--idempotency-key` to get the first receipt back |

`invoke deploy`, `invoke dev` and `invoke agents` still work on projects made by earlier versions,
but they are no longer listed in the help.

`invoke gateway call` no longer sends `--agent-id cli_agent` by default. With an agent's own key
the key decides who acts, and naming another agent is refused. Pass `--agent-id` only to act as a
registered agent with an org key.

## Links

- Docs: https://docs.invokehq.run
- Console: https://console.invokehq.run/sign-in
- License: Apache-2.0
