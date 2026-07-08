#!/usr/bin/env python3
"""Generate small MCP wrappers for Invoke.

Examples:
  invoke wrap postgresql --query "SELECT * FROM users WHERE id = :user_id"
  invoke wrap my-fastapi-app --openapi openapi.json --base-url http://localhost:8000
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import re
import socket
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any


HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
LAUNCH_CONNECTORS = {"github", "notion", "linear"}
HOSTED_API_URL = "https://api.invokehq.run"
DEFAULT_API_URL = HOSTED_API_URL
DEFAULT_TIMEOUT_SECONDS = int(os.getenv("INVOKE_TIMEOUT_SECONDS", "90"))


class CliUsageError(ValueError):
    """User-facing CLI error that should print without a Python traceback."""


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "wrapped-tool"


def tool_name(value: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").lower()
    return name or "wrapped_tool"


def invoke_home() -> Path:
    return Path(os.getenv("INVOKE_HOME", Path.home() / ".invoke"))


def credentials_path() -> Path:
    return invoke_home() / "config.json"


def legacy_credentials_path() -> Path:
    return invoke_home() / "credentials.json"


def deployments_path() -> Path:
    return invoke_home() / "deployments.json"


def dev_runtime_path(project_root: Path) -> Path:
    return project_root / ".invoke" / "dev.json"


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc


def write_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def package_version() -> str:
    package_path = Path(__file__).with_name("package.json")
    try:
        data = load_json_file(package_path, {})
    except ValueError:
        data = {}
    if isinstance(data, dict) and data.get("version"):
        return str(data["version"])
    return "dev"


def project_context(root: Path) -> str:
    context_path = root / ".invoke" / "context.json"
    if context_path.exists():
        try:
            context = load_json_file(context_path, {})
        except ValueError:
            return str(context_path)
        if isinstance(context, dict):
            name = context.get("slug") or context.get("name") or context.get("target")
            return str(name or context_path)
    try:
        config = read_project(root)
    except Exception:
        return ""
    return str(config.get("slug") or config.get("name") or root.name)


def is_placeholder_mcp_url(value: str | None) -> bool:
    return not value or "replace-with-your" in value


def mask_key(value: str) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 12:
        return value[:4] + "..."
    return value[:8] + "..." + value[-4:]


def load_credentials() -> dict[str, str]:
    stored = load_json_file(credentials_path(), {})
    if not stored and legacy_credentials_path().exists():
        stored = load_json_file(legacy_credentials_path(), {})
    if not isinstance(stored, dict):
        stored = {}
    return {
        "base_url": (
            os.getenv("INVOKE_BASE_URL")
            or stored.get("base_url")
            or stored.get("baseUrl")
            or DEFAULT_API_URL
        ),
        "api_key": os.getenv("INVOKE_API_KEY") or stored.get("api_key") or stored.get("apiKey") or "",
    }


def load_config_store() -> dict[str, Any]:
    """Raw config.json contents (base_url, api_key, workspace_id, ...), legacy-aware."""
    stored = load_json_file(credentials_path(), {})
    if not stored and legacy_credentials_path().exists():
        stored = load_json_file(legacy_credentials_path(), {})
    return stored if isinstance(stored, dict) else {}


def save_config_store(store: dict[str, Any]) -> None:
    store = dict(store)
    store["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    write_json_file(credentials_path(), store)


def write_credentials(base_url: str, api_key: str) -> None:
    cleaned_base_url = base_url.rstrip("/")
    cleaned_api_key = api_key.strip()
    store = load_config_store()
    store.update(
        {
            "base_url": cleaned_base_url,
            "baseUrl": cleaned_base_url,
            "api_key": cleaned_api_key,
            "apiKey": cleaned_api_key,
        }
    )
    save_config_store(store)


def stored_workspace() -> str | None:
    store = load_config_store()
    value = store.get("workspace_id") or store.get("workspaceId")
    return str(value) if value else None


def set_active_workspace(workspace_id: str) -> None:
    store = load_config_store()
    store["workspace_id"] = workspace_id
    save_config_store(store)


def project_workspace(root: Path) -> str | None:
    """Workspace id pinned in a project's invoke.json, if any."""
    try:
        config = load_json_file(root / "invoke.json", {})
    except ValueError:
        return None
    if isinstance(config, dict):
        value = config.get("workspace_id") or config.get("workspaceId")
        return str(value) if value else None
    return None


def load_workspace(args: argparse.Namespace) -> str:
    """Resolve the active workspace id: --workspace > env > project invoke.json > config."""
    workspace_id = (
        getattr(args, "workspace", None)
        or os.getenv("INVOKE_WORKSPACE")
        or project_workspace(Path.cwd())
        or stored_workspace()
    )
    if not workspace_id:
        raise CliUsageError(
            "No Invoke workspace selected. Run `invoke init <name>` to provision one, "
            "set INVOKE_WORKSPACE, or pass --workspace <id>."
        )
    return workspace_id


def normalize_config_key(key: str) -> str:
    aliases = {
        "base-url": "base_url",
        "base_url": "base_url",
        "baseUrl": "base_url",
        "api-key": "api_key",
        "api_key": "api_key",
        "apiKey": "api_key",
    }
    if key not in aliases:
        raise CliUsageError("Config key must be base-url or api-key.")
    return aliases[key]


def runtime_error_hint(base_url: str) -> str:
    return textwrap.dedent(
        f"""\

        Runtime: {base_url}

        Fix:
          invoke login --base-url {HOSTED_API_URL} --api-key <your_key>

        Local dev:
          python main.py
          invoke login --base-url http://localhost:8000 --api-key <your_key>

        You can also set INVOKE_BASE_URL and INVOKE_API_KEY.
        """
    ).rstrip()


def api_request(method: str, base_url: str, path: str, api_key: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401:
            raise RuntimeError(
                f"{method} {url} failed with 401: invalid API key.\n"
                "Run `invoke login --api-key <your_key>` or set INVOKE_API_KEY."
            ) from exc
        if exc.code == 404:
            raise RuntimeError(
                f"{method} {url} failed with 404: endpoint not found.\n"
                "Your CLI may be pointed at an old runtime. "
                f"Run `invoke login --base-url {HOSTED_API_URL} --api-key <your_key>`."
            ) from exc
        raise RuntimeError(f"{method} {url} failed with {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        if "Name or service not known" in reason or "getaddrinfo" in reason or "nodename" in reason:
            raise RuntimeError(
                f"Could not resolve Invoke runtime host for {base_url}.\n"
                f"{runtime_error_hint(base_url)}"
            ) from exc
        raise RuntimeError(
            f"Could not reach Invoke runtime at {base_url}: {reason}\n"
            f"{runtime_error_hint(base_url)}"
        ) from exc
    except socket.timeout as exc:
        raise RuntimeError(
            f"{method} {url} timed out after {DEFAULT_TIMEOUT_SECONDS}s.\n"
            "The runtime may be cold-starting or the upstream tool may be slow. Try again, or set "
            "INVOKE_TIMEOUT_SECONDS=120 for heavier calls."
        ) from exc


def require_credentials(args: argparse.Namespace) -> dict[str, str]:
    credentials = load_credentials()
    base_url = getattr(args, "base_url", None) or credentials["base_url"]
    api_key = getattr(args, "api_key", None) or credentials["api_key"]
    if not api_key:
        raise CliUsageError("Missing API key. Run `invoke login --api-key ...` or set INVOKE_API_KEY.")
    return {"base_url": base_url, "api_key": api_key}


def runtime_request(
    method: str,
    credentials: dict[str, str],
    workspace_id: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call the workspace-scoped runtime router (/v1/runtime/workspaces/{id}...)."""
    prefix = f"/v1/runtime/workspaces/{workspace_id}"
    return api_request(method, credentials["base_url"], f"{prefix}{path}", credentials["api_key"], body)


def infer_sql_params(query: str) -> dict[str, Any]:
    names = sorted(set(re.findall(r":([a-zA-Z_][a-zA-Z0-9_]*)", query)))
    properties = {
        name: {
            "type": "string",
            "description": f"Value for SQL parameter :{name}.",
        }
        for name in names
    }
    return {"type": "object", "properties": properties, "required": names}


def sql_is_read_only(query: str) -> bool:
    stripped = re.sub(r"/\*.*?\*/", "", query, flags=re.S).strip()
    stripped = re.sub(r"--.*?$", "", stripped, flags=re.M).strip()
    return stripped.lower().startswith(("select", "with"))


def postgres_tool(args: argparse.Namespace) -> dict[str, Any]:
    if not args.query:
        raise ValueError("postgresql wrappers require --query")
    read_only = sql_is_read_only(args.query)
    if not read_only and not args.allow_write:
        raise ValueError("postgresql wrappers are read-only by default; pass --allow-write for mutating SQL")

    name = tool_name(args.name or "postgres_query")
    description = args.description or "Run a scoped PostgreSQL query and return rows as JSON."
    return {
        "name": name,
        "description": description,
        "input_schema": infer_sql_params(args.query),
        "output_schema": {
            "type": "object",
            "properties": {
                "rows": {"type": "array", "items": {"type": "object"}},
                "row_count": {"type": "integer"},
            },
            "required": ["rows", "row_count"],
        },
        "annotations": {
            "title": args.name or "PostgreSQL Query",
            "readOnlyHint": read_only,
            "idempotentHint": read_only,
            "openWorldHint": False,
        },
        "idempotency": {
            "mode": "automatic" if read_only else "caller_provided",
            "key_fields": sorted(infer_sql_params(args.query)["properties"].keys()),
        },
        "retry": {
            "safe": read_only,
            "max_attempts": 2 if read_only else 1,
            "backoff_ms": 250,
        },
        "x-invoke": {
            "kind": "postgresql",
            "query": args.query,
            "read_only": read_only,
            "database_url_env": args.database_url_env,
        },
    }


def schema_for_parameter(parameter: dict[str, Any]) -> dict[str, Any]:
    schema = parameter.get("schema") if isinstance(parameter.get("schema"), dict) else {}
    return {
        "type": schema.get("type", "string"),
        "description": parameter.get("description", ""),
    }


def fastapi_tools_from_openapi(args: argparse.Namespace) -> list[dict[str, Any]]:
    with open(args.openapi, "r", encoding="utf-8") as fh:
        spec = json.load(fh)

    tools: list[dict[str, Any]] = []
    for path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue

            op_name = tool_name(operation.get("operationId") or f"{method}_{path}")
            properties: dict[str, Any] = {}
            required: list[str] = []
            parameter_locations: dict[str, str] = {}

            for parameter in operation.get("parameters", []):
                if not isinstance(parameter, dict) or "name" not in parameter:
                    continue
                name = str(parameter["name"])
                properties[name] = schema_for_parameter(parameter)
                parameter_locations[name] = str(parameter.get("in", "query"))
                if parameter.get("required"):
                    required.append(name)

            request_body = operation.get("requestBody", {})
            json_body = (
                request_body.get("content", {})
                .get("application/json", {})
                .get("schema")
                if isinstance(request_body, dict)
                else None
            )
            if isinstance(json_body, dict):
                properties["body"] = json_body
                if request_body.get("required"):
                    required.append("body")

            description = operation.get("summary") or operation.get("description") or f"Call {method.upper()} {path}."
            tools.append(
                {
                    "name": op_name,
                    "description": description,
                    "input_schema": {"type": "object", "properties": properties, "required": required},
                    "output_schema": {"type": "object"},
                    "annotations": {
                        "title": operation.get("summary") or op_name,
                        "readOnlyHint": method.lower() == "get",
                        "idempotentHint": method.lower() in {"get", "put", "delete"},
                        "openWorldHint": True,
                    },
                    "idempotency": {
                        "mode": "automatic" if method.lower() in {"get", "put", "delete"} else "caller_provided",
                        "key_fields": required,
                    },
                    "retry": {
                        "safe": method.lower() in {"get", "put", "delete"},
                        "max_attempts": 2 if method.lower() in {"get", "put", "delete"} else 1,
                        "backoff_ms": 250,
                    },
                    "x-invoke": {
                        "kind": "http",
                        "method": method.upper(),
                        "path": path,
                        "base_url": args.base_url,
                        "parameter_locations": parameter_locations,
                    },
                }
            )
    if not tools:
        raise ValueError(f"No HTTP operations found in {args.openapi}")
    return tools


def generic_fastapi_tool(args: argparse.Namespace) -> dict[str, Any]:
    name = tool_name(args.name or f"{args.target}_request")
    return {
        "name": name,
        "description": args.description or f"Call a scoped endpoint on {args.target}.",
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "default": "GET"},
                "path": {"type": "string", "description": "Path beginning with /."},
                "query": {"type": "object", "default": {}},
                "body": {"type": "object", "default": {}},
            },
            "required": ["path"],
        },
        "output_schema": {"type": "object"},
        "annotations": {"title": args.name or args.target, "readOnlyHint": False, "idempotentHint": False},
        "idempotency": {"mode": "caller_provided", "key_fields": ["method", "path", "query", "body"]},
        "retry": {"safe": False, "max_attempts": 1, "backoff_ms": 250},
        "x-invoke": {
            "kind": "http_generic",
            "base_url": args.base_url,
        },
    }


def connector_tool(
    *,
    name: str,
    title: str,
    description: str,
    input_schema: dict[str, Any],
    method: str,
    base_url: str,
    path: str,
    auth_env: str,
    headers: dict[str, str] | None = None,
    parameter_locations: dict[str, str] | None = None,
    body_fields: list[str] | None = None,
    body_template: dict[str, Any] | None = None,
    graphql_query: str | None = None,
    graphql_variables: list[str] | None = None,
) -> dict[str, Any]:
    kind = "graphql" if graphql_query else "http"
    config: dict[str, Any] = {
        "kind": kind,
        "method": method,
        "base_url": base_url,
        "path": path,
        "headers": headers or {},
        "parameter_locations": parameter_locations or {},
        "body_fields": body_fields or [],
        "auth": {"env": auth_env, "scheme": "Bearer"},
    }
    if body_template:
        config["body_template"] = body_template
    if graphql_query:
        config["query"] = graphql_query
        config["variables"] = graphql_variables or []

    return {
        "name": name,
        "description": description,
        "input_schema": input_schema,
        "output_schema": {"type": "object"},
        "annotations": {
            "title": title,
            "readOnlyHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
        "idempotency": {"mode": "caller_provided", "key_fields": input_schema.get("required", [])},
        "retry": {"safe": False, "max_attempts": 1, "backoff_ms": 250},
        "x-invoke": config,
    }


def launch_connector_tools(target: str) -> list[dict[str, Any]]:
    normalized = slugify(target)
    if normalized == "github":
        return [
            connector_tool(
                name="github_create_issue",
                title="GitHub Create Issue",
                description="Create a GitHub issue after Invoke approval.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string", "description": "Repository owner."},
                        "repo": {"type": "string", "description": "Repository name."},
                        "title": {"type": "string", "description": "Issue title."},
                        "body": {"type": "string", "description": "Issue body."},
                        "labels": {"type": "array", "items": {"type": "string"}, "default": []},
                    },
                    "required": ["owner", "repo", "title"],
                },
                method="POST",
                base_url="https://api.github.com",
                path="/repos/{owner}/{repo}/issues",
                auth_env="GITHUB_TOKEN",
                headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
                parameter_locations={"owner": "path", "repo": "path"},
                body_fields=["title", "body", "labels"],
            )
        ]
    if normalized == "notion":
        return [
            connector_tool(
                name="notion_create_page",
                title="Notion Create Page",
                description="Create a Notion page with title and optional paragraph content.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "parent_id": {"type": "string", "description": "Parent page or database ID."},
                        "title": {"type": "string", "description": "Page title."},
                        "content": {"type": "string", "description": "Plain text paragraph content."},
                    },
                    "required": ["parent_id", "title"],
                },
                method="POST",
                base_url="https://api.notion.com",
                path="/v1/pages",
                auth_env="NOTION_TOKEN",
                headers={"Notion-Version": "2022-06-28"},
                body_template={
                    "parent": {"page_id": "{parent_id}"},
                    "properties": {
                        "title": {
                            "title": [{"text": {"content": "{title}"}}],
                        }
                    },
                    "children": [
                        {
                            "object": "block",
                            "type": "paragraph",
                            "paragraph": {"rich_text": [{"type": "text", "text": {"content": "{content}"}}]},
                        }
                    ],
                },
            )
        ]
    if normalized == "linear":
        return [
            connector_tool(
                name="linear_create_issue",
                title="Linear Create Issue",
                description="Create a Linear issue in a scoped team.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "team_id": {"type": "string", "description": "Linear team ID."},
                        "title": {"type": "string", "description": "Issue title."},
                        "description": {"type": "string", "description": "Issue description."},
                    },
                    "required": ["team_id", "title"],
                },
                method="POST",
                base_url="https://api.linear.app/graphql",
                path="",
                auth_env="LINEAR_API_KEY",
                graphql_query=(
                    "mutation InvokeCreateIssue($team_id: String!, $title: String!, $description: String) "
                    "{ issueCreate(input: {teamId: $team_id, title: $title, description: $description}) "
                    "{ success issue { id identifier title url } } }"
                ),
                graphql_variables=["team_id", "title", "description"],
            )
        ]
    raise ValueError("launch connectors are github, notion, or linear")


def server_template(tools: list[dict[str, Any]]) -> str:
    # Embed the tool table as a Python literal (True/False/None) — NOT raw JSON.
    # json.dumps emits true/false/null, which are undefined names in Python and make
    # the generated server.py raise NameError on import. pprint.pformat renders valid
    # Python literals while keeping the table readable.
    import pprint

    tools_json = pprint.pformat(
        {tool["name"]: tool for tool in tools}, indent=2, sort_dicts=True, width=100
    )
    return (
        '''#!/usr/bin/env python3
"""Generated Invoke MCP wrapper."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


TOOLS = '''
        + tools_json
        + r'''

app = FastAPI(title="Generated Invoke MCP Wrapper", version="0.1.0")


def jsonrpc_result(request_id: Any, result: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def jsonrpc_error(request_id: Any, code: int, message: str, *, retryable: bool = False, details: Any = None) -> JSONResponse:
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": code,
                "message": message,
                "data": {
                    "retryable": retryable,
                    "details": details,
                },
            },
        }
    )


def validate_value(name: str, schema: dict[str, Any], value: Any) -> str | None:
    expected = schema.get("type")
    if expected == "string" and not isinstance(value, str):
        return f"{name} must be a string"
    if expected == "integer" and not isinstance(value, int):
        return f"{name} must be an integer"
    if expected == "number" and not isinstance(value, (int, float)):
        return f"{name} must be a number"
    if expected == "boolean" and not isinstance(value, bool):
        return f"{name} must be a boolean"
    if expected == "object" and not isinstance(value, dict):
        return f"{name} must be an object"
    if expected == "array" and not isinstance(value, list):
        return f"{name} must be an array"
    enum = schema.get("enum")
    if enum and value not in enum:
        return f"{name} must be one of {enum}"
    return None


def validate_args(tool: dict[str, Any], args: dict[str, Any]) -> list[str]:
    schema = tool.get("input_schema", {})
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    errors = []
    for name in required:
        if name not in args:
            errors.append(f"{name} is required")
    for name, value in args.items():
        prop_schema = properties.get(name)
        if isinstance(prop_schema, dict):
            error = validate_value(name, prop_schema, value)
            if error:
                errors.append(error)
    return errors


async def call_postgresql(tool: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("Install psycopg to run this PostgreSQL wrapper: pip install psycopg[binary]") from exc

    config = tool["x-invoke"]
    database_url = os.getenv(config.get("database_url_env", "DATABASE_URL"))
    if not database_url:
        raise RuntimeError(f"Missing {config.get('database_url_env', 'DATABASE_URL')}")

    query = config["query"]
    if config.get("read_only") and not re.sub(r"/\*.*?\*/", "", query, flags=re.S).strip().lower().startswith(("select", "with")):
        raise RuntimeError("Generated PostgreSQL wrapper refused non-read-only SQL")

    started = time.perf_counter()
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(query, args)
            rows = cur.fetchall() if cur.description else []
    return {
        "rows": rows,
        "row_count": len(rows),
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def interpolate_path(path: str, args: dict[str, Any], locations: dict[str, str]) -> str:
    for name, location in locations.items():
        if location == "path" and name in args:
            path = path.replace("{" + name + "}", str(args[name]))
    return path


def render_template_value(value: Any, args: dict[str, Any]) -> Any:
    if isinstance(value, str):
        for key, arg_value in args.items():
            value = value.replace("{" + key + "}", "" if arg_value is None else str(arg_value))
        return value
    if isinstance(value, list):
        return [render_template_value(item, args) for item in value]
    if isinstance(value, dict):
        return {key: render_template_value(item, args) for key, item in value.items()}
    return value


def request_headers(config: dict[str, Any]) -> dict[str, str]:
    headers = dict(config.get("headers", {}))
    auth = config.get("auth") or {}
    if auth:
        token = os.getenv(auth.get("env", "API_TOKEN"))
        if not token:
            raise RuntimeError(f"Missing {auth.get('env', 'API_TOKEN')}")
        header_name = auth.get("header", "Authorization")
        scheme = auth.get("scheme", "Bearer")
        headers[header_name] = f"{scheme} {token}" if scheme else token
    return headers


async def call_http(tool: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    config = tool["x-invoke"]
    method = config.get("method", args.get("method", "GET")).upper()
    base_url = config["base_url"].rstrip("/")
    locations = config.get("parameter_locations", {})
    path = interpolate_path(config.get("path", args.get("path", "/")), args, locations)
    query = args.get("query", {}) if config.get("kind") == "http_generic" else {
        name: value for name, value in args.items() if locations.get(name) == "query"
    }
    if config.get("body_template"):
        body = render_template_value(config["body_template"], args)
    elif config.get("body_fields"):
        body = {name: args[name] for name in config["body_fields"] if name in args}
    else:
        body = args.get("body", {}) if config.get("kind") == "http_generic" else args.get("body")

    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.request(method, f"{base_url}{path}", params=query, json=body, headers=request_headers(config))
    content_type = response.headers.get("content-type", "")
    payload = response.json() if "application/json" in content_type else {"text": response.text}
    return {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "body": payload,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


async def call_graphql(tool: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    config = tool["x-invoke"]
    variables = {name: args.get(name) for name in config.get("variables", []) if name in args}
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            config["base_url"],
            json={"query": config["query"], "variables": variables},
            headers=request_headers(config),
        )
    content_type = response.headers.get("content-type", "")
    payload = response.json() if "application/json" in content_type else {"text": response.text}
    return {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "body": payload,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


async def dispatch_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    tool = TOOLS.get(name)
    if not tool:
        raise KeyError(f"Unknown tool {name}")
    errors = validate_args(tool, args)
    if errors:
        raise ValueError("; ".join(errors))
    kind = tool.get("x-invoke", {}).get("kind")
    if kind == "postgresql":
        return await call_postgresql(tool, args)
    if kind in {"http", "http_generic"}:
        return await call_http(tool, args)
    if kind == "graphql":
        return await call_graphql(tool, args)
    raise RuntimeError(f"Unsupported generated tool kind {kind}")


@app.post("/mcp")
async def mcp(request: Request):
    payload = await request.json()
    request_id = payload.get("id")
    method = payload.get("method")

    if method == "initialize":
        return jsonrpc_result(
            request_id,
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "invoke-generated-wrapper", "version": "0.1.0"},
            },
        )
    if method == "notifications/initialized":
        return JSONResponse(status_code=202, content={})
    if method == "tools/list":
        return jsonrpc_result(
            request_id,
            {
                "tools": [
                    {
                        "name": name,
                        "description": tool["description"],
                        "inputSchema": tool["input_schema"],
                        "annotations": tool.get("annotations", {}),
                    }
                    for name, tool in TOOLS.items()
                ]
            },
        )
    if method == "tools/call":
        params = payload.get("params", {})
        name = params.get("name")
        args = params.get("arguments", {})
        correlation_id = request.headers.get("idempotency-key") or str(uuid.uuid4())
        try:
            result = await dispatch_tool(name, args)
            result["_invoke"] = {
                "correlation_id": correlation_id,
                "idempotency": TOOLS[name].get("idempotency", {}),
                "retry": TOOLS[name].get("retry", {}),
            }
            return jsonrpc_result(request_id, result)
        except ValueError as exc:
            return jsonrpc_error(request_id, -32602, str(exc), retryable=False)
        except KeyError as exc:
            return jsonrpc_error(request_id, -32601, str(exc), retryable=False)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return jsonrpc_error(request_id, -32001, str(exc), retryable=True)
        except Exception as exc:
            return jsonrpc_error(request_id, -32000, str(exc), retryable=False)

    return jsonrpc_error(request_id, -32601, f"Unsupported method {method}", retryable=False)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8787")))
'''
    )


def registration_payload(slug: str, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": tool["name"],
            "capability_card": {
                "name": tool.get("annotations", {}).get("title") or tool["name"],
                "description": tool["description"],
                "capability": f"{slug}.{tool['name']}",
                "input_schema": tool["input_schema"],
                "output_schema": tool.get("output_schema", {}),
                "idempotency": tool.get("idempotency", {}),
                "retry": tool.get("retry", {}),
                "tags": ["generated", slug],
            },
            "mcp_tool": tool["name"],
            "mcp_url": "https://replace-with-your-hosted-wrapper.example.com/mcp",
            "approval_required": not bool(tool.get("annotations", {}).get("readOnlyHint")),
            "risk_level": "low" if tool.get("annotations", {}).get("readOnlyHint") else "medium",
        }
        for tool in tools
    ]


def write_project(slug: str, tools: list[dict[str, Any]], output: str) -> Path:
    root = Path(output) / slug
    root.mkdir(parents=True, exist_ok=True)
    (root / "server.py").write_text(server_template(tools), encoding="utf-8")
    (root / "capability.json").write_text(
        json.dumps({"name": slug, "mcp_endpoint": "/mcp", "tools": tools}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (root / "invoke.register.json").write_text(
        json.dumps(registration_payload(slug, tools), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    requirements = ["fastapi", "uvicorn[standard]", "httpx"]
    if any(tool.get("x-invoke", {}).get("kind") == "postgresql" for tool in tools):
        requirements.append("psycopg[binary]")
    (root / "requirements.txt").write_text("\n".join(requirements) + "\n", encoding="utf-8")
    env_vars = sorted(
        {
            tool.get("x-invoke", {}).get("auth", {}).get("env")
            for tool in tools
            if tool.get("x-invoke", {}).get("auth", {}).get("env")
        }
    )
    env_text = "\n".join(f"- `{env}`" for env in env_vars) if env_vars else "- none"
    (root / "README.md").write_text(
        textwrap.dedent(
            f"""\
            # {slug}

            Generated Invoke MCP wrapper.

            Required environment variables:

            {env_text}

            Run locally:

            ```bash
            pip install -r requirements.txt
            python server.py
            ```

            Register with Invoke by POSTing each object in `invoke.register.json` to:

            ```text
            /providers/<provider_id>/tools
            ```

            Replace `mcp_url` with the hosted `/mcp` URL after deploying this wrapper.
            """
        ),
        encoding="utf-8",
    )
    return root


def build_tools(args: argparse.Namespace) -> tuple[str, list[dict[str, Any]]]:
    if args.target == "postgresql":
        return slugify(args.name or "postgresql-query"), [postgres_tool(args)]
    if slugify(args.target) in LAUNCH_CONNECTORS:
        return slugify(args.name or args.target), launch_connector_tools(args.target)
    if args.openapi:
        return slugify(args.name or args.target), fastapi_tools_from_openapi(args)
    return slugify(args.name or args.target), [generic_fastapi_tool(args)]


def wrap_command(args: argparse.Namespace) -> int:
    slug, tools = build_tools(args)
    root = write_project(slug, tools, args.output)
    print(json.dumps({"success": True, "path": str(root), "tools": [tool["name"] for tool in tools]}, indent=2))
    return 0


def login_command(args: argparse.Namespace) -> int:
    base_url = args.base_url or os.getenv("INVOKE_BASE_URL") or DEFAULT_API_URL
    api_key = args.api_key or os.getenv("INVOKE_API_KEY")
    if not api_key:
        if not sys.stdin.isatty():
            raise ValueError("Pass --api-key in non-interactive shells.")
        api_key = getpass.getpass("Invoke API key: ").strip()
    if not api_key:
        raise ValueError("API key is required")

    write_credentials(base_url, api_key)
    print(f"Logged in to {base_url.rstrip('/')}")
    print(f"Credentials saved to {credentials_path()}")
    return 0


def config_command(args: argparse.Namespace) -> int:
    credentials = load_credentials()
    config = {
        "base_url": credentials["base_url"],
        "api_key": credentials["api_key"],
        "credentials_path": str(credentials_path()),
        "home": str(invoke_home()),
    }
    if args.key:
        normalized_key = normalize_config_key(args.key)
        if args.value is None:
            print(config[normalized_key])
            return 0
        updated = dict(credentials)
        updated[normalized_key] = args.value.strip()
        write_credentials(updated["base_url"], updated["api_key"])
        print(f"Updated {args.key} in {credentials_path()}")
        return 0

    if args.json:
        safe = dict(config)
        if safe["api_key"]:
            safe["api_key"] = mask_key(safe["api_key"])
        print(json.dumps(safe, indent=2))
        return 0

    print(f"Base URL: {credentials['base_url']}")
    print(f"API key: {mask_key(credentials['api_key'])}")
    print(f"Config: {credentials_path()}")
    return 0


def fmt_cost(micros: Any) -> str:
    try:
        return f"${int(micros) / 1_000_000:,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def fmt_pct(rate: Any) -> str:
    try:
        return f"{float(rate) * 100:.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def event_clock(event: dict[str, Any]) -> str:
    raw = event.get("at") or event.get("ts")
    if isinstance(raw, str):
        try:
            return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%H:%M:%S")
        except ValueError:
            return raw
    if isinstance(raw, (int, float)):
        return dt.datetime.fromtimestamp(raw, dt.timezone.utc).strftime("%H:%M:%S")
    return "--:--:--"


# Friendly labels for the ledger event types the log renders.
EVENT_LABELS: dict[str, str] = {
    "workspace.created": "Workspace created",
    "budget.set": "Budget set",
    "tool.registered": "Tool registered",
    "agent.registered": "Agent registered",
    "agent.heartbeat": "Agent heartbeat",
    "agent.retired": "Agent retired",
    "task.created": "Task created",
    "task.assigned": "Task assigned",
    "task.claimed": "Task claimed",
    "task.handed_off": "Handoff",
    "task.status_changed": "Task status changed",
    "execution.requested": "Execution requested",
    "execution.started": "Execution started",
    "execution.completed": "Completed",
    "execution.failed": "Failed",
    "execution.denied": "Denied",
    "execution.deduplicated": "Deduplicated (reconciled)",
    "approval.requested": "Approval required",
    "approval.granted": "Approved",
    "approval.denied": "Rejected",
    "receipt.issued": "Receipt issued",
    "context.written": "Context written",
    "message.broadcast": "Broadcast",
}


def event_summary(event: dict[str, Any]) -> str:
    etype = event.get("type", "event")
    label = EVENT_LABELS.get(etype, etype)
    tool = event.get("tool") or (event.get("payload") or {}).get("tool")
    if tool and etype.startswith("execution"):
        return f"{label}: {tool}"
    return label


def status_plain(base_url: str, api_key: str, check: bool) -> int:
    print("Invoke CLI status")
    print(f"Version: {package_version()}")
    print(f"Runtime: {base_url}")
    print(f"API key: {mask_key(api_key)}")
    print(f"Config: {credentials_path()}")
    print(f"Workspace: {stored_workspace() or '(none)'}")
    project = project_context(Path.cwd())
    print(f"Project: {project or '(none)'}")
    if check:
        if not api_key:
            raise CliUsageError("Cannot check runtime without an API key. Run `invoke login --api-key ...` first.")
        health = api_request("GET", base_url, "/health", api_key)
        print(f"Health: {health.get('status', 'ok')}")
        if health.get("db"):
            print(f"DB: {health.get('db')}")
    return 0


def status_command(args: argparse.Namespace) -> int:
    credentials = load_credentials()
    base_url = args.base_url or credentials["base_url"]
    api_key = args.api_key or credentials["api_key"]
    creds = {"base_url": base_url, "api_key": api_key}

    try:
        workspace_id = load_workspace(args)
    except CliUsageError:
        workspace_id = None

    if args.plain or not workspace_id:
        if not workspace_id and not args.plain:
            print("No workspace selected — showing login context. Run `invoke init <name>` to provision one.\n")
        return status_plain(base_url, api_key, args.check)

    if not api_key:
        raise CliUsageError("Missing API key. Run `invoke login --api-key ...` or set INVOKE_API_KEY.")

    health: dict[str, Any] = {}
    try:
        health = api_request("GET", base_url, "/health", api_key)
    except RuntimeError:
        health = {}
    overview = runtime_request("GET", creds, workspace_id, "/overview").get("overview", {})
    metrics = runtime_request("GET", creds, workspace_id, "/metrics?window_minutes=1440").get("metrics", {})

    if args.json:
        print(json.dumps({"health": health, "overview": overview, "metrics": metrics}, indent=2))
        return 0

    totals = metrics.get("totals", {}) if isinstance(metrics, dict) else {}
    agents = overview.get("agents", {}) if isinstance(overview, dict) else {}
    approvals = overview.get("approvals", {}) if isinstance(overview, dict) else {}
    ws_name = (overview.get("workspace") or {}).get("name") or workspace_id
    recovery = 1.0 - float(totals.get("failure_rate", 0.0) or 0.0)
    raw_health = str(health.get("status", "unknown")) if health else "unreachable"
    health_status = {"ok": "Healthy"}.get(raw_health, raw_health.capitalize())

    print(f"Workspace:        {ws_name} ({workspace_id})")
    print(f"Runtime:          {health_status}")
    print(f"Agents:           {agents.get('total', 0)} ({agents.get('live', 0)} live)")
    print(f"Executions (24h): {int(totals.get('requested', 0)):,}")
    print(f"Recovery Rate:    {fmt_pct(recovery)}")
    print(f"Cost (24h):       {fmt_cost(totals.get('spend_micros', 0))}")
    pending = approvals.get("pending", 0)
    if pending:
        print(f"Approvals:        {pending} pending  →  invoke approvals")
    return 0


def print_events(events: list[dict[str, Any]]) -> None:
    for event in events:
        print(f"{event_clock(event)}  {event_summary(event)}")


def logs_command(args: argparse.Namespace) -> int:
    creds = require_credentials(args)
    workspace_id = load_workspace(args)
    query = f"/events?after_seq={args.since}&limit={args.limit}&order=asc"
    if args.types:
        query += f"&types={urllib.parse.quote(args.types)}"
    response = runtime_request("GET", creds, workspace_id, query)
    events = response.get("events", [])

    if args.json and not args.follow:
        print(json.dumps(events, indent=2))
        return 0

    print_events(events)
    if not args.follow:
        if not events:
            print("(no events yet — run `invoke run <agent>` to produce some)")
        return 0

    last_seq = response.get("next_after_seq") or (events[-1]["seq"] if events else args.since)
    print("… following (Ctrl-C to stop)")
    try:
        while True:
            time.sleep(2)
            follow_query = f"/events?after_seq={last_seq}&limit=100&order=asc"
            if args.types:
                follow_query += f"&types={urllib.parse.quote(args.types)}"
            batch = runtime_request("GET", creds, workspace_id, follow_query)
            new_events = batch.get("events", [])
            if new_events:
                print_events(new_events)
                last_seq = batch.get("next_after_seq") or new_events[-1]["seq"]
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


TERMINAL_TASK_STATUS = {"done", "cancelled", "failed"}


def watch_task(creds: dict[str, str], workspace_id: str, task_id: str, timeout_seconds: int = 15) -> int:
    """Follow a task's events + status until it settles or we time out."""
    deadline = time.time() + timeout_seconds
    last_seq = 0
    last_status: str | None = None
    while time.time() < deadline:
        batch = runtime_request(
            "GET", creds, workspace_id, f"/events?task_id={task_id}&after_seq={last_seq}&limit=100&order=asc"
        )
        events = batch.get("events", [])
        if events:
            print_events(events)
            last_seq = batch.get("next_after_seq") or events[-1]["seq"]
        task = runtime_request("GET", creds, workspace_id, f"/tasks/{task_id}").get("task", {})
        status = task.get("status")
        if status and status != last_status:
            last_status = status
            print(f"  · status: {status}")
        if status in TERMINAL_TASK_STATUS:
            print(f"✔ Task {status}")
            return 0 if status == "done" else 1
        time.sleep(1.5)
    print(f"… still {last_status or 'queued'} — watch with `invoke logs -f` or `invoke status`.")
    return 0


def run_command(args: argparse.Namespace) -> int:
    creds = require_credentials(args)
    workspace_id = load_workspace(args)

    target = args.target
    title = target
    payload: dict[str, Any] = {}
    file_path = Path(target)
    if file_path.exists() and file_path.suffix.lower() == ".json":
        loaded = load_json_file(file_path, {})
        payload = loaded if isinstance(loaded, dict) else {"input": loaded}
        title = file_path.stem
    elif file_path.suffix.lower() in {".yaml", ".yml"} and file_path.exists():
        raise CliUsageError(
            f"{target} is YAML; the CLI reads JSON specs. Convert it to .json, or pass params with --params."
        )
    else:
        try:
            parsed = json.loads(args.params)
        except json.JSONDecodeError as exc:
            raise CliUsageError(f"--params must be a JSON object: {exc}") from exc
        payload = parsed if isinstance(parsed, dict) else {"input": parsed}

    body: dict[str, Any] = {"title": title, "payload": payload}
    response = runtime_request("POST", creds, workspace_id, "/tasks", body)
    task = response.get("task", {})
    task_id = task.get("id")

    if args.json:
        print(json.dumps(response, indent=2))
        return 0

    print(f"▶ Started task {task_id}: {title}")
    print(f"  status: {task.get('status', 'queued')}")
    if args.detach or not task_id:
        print("  (detached) follow with: invoke logs -f")
        return 0
    return watch_task(creds, workspace_id, task_id)


def doctor_command(args: argparse.Namespace) -> int:
    credentials = load_credentials()
    base_url = args.base_url or credentials["base_url"]
    api_key = args.api_key or credentials["api_key"]

    print("Invoke doctor")
    print(f"Runtime: {base_url}")
    print(f"API key: {mask_key(api_key)}")
    print(f"Timeout: {DEFAULT_TIMEOUT_SECONDS}s")
    print(f"Config: {credentials_path()}")

    if not api_key:
        print("\nMissing API key.")
        print("Run: invoke login --base-url https://api.invokehq.run --api-key <your_key>")
        return 1

    try:
        health = api_request("GET", base_url, "/health", api_key)
    except RuntimeError as exc:
        print(f"\nRuntime check failed:\n{exc}")
        return 1

    print(f"\nHealth: {health.get('status', 'ok')}")
    if health.get("db"):
        print(f"DB: {health.get('db')}")
    return 0


def project_template(name: str, template: str) -> dict[str, Any]:
    slug = slugify(name)
    base = {
        "name": name,
        "slug": slug,
        "version": "0.1.0",
        "owner_email": "dev@example.com",
        "mcp_url": "https://replace-with-your-mcp-server.example.com/mcp",
    }
    if template == "linear":
        base["tools"] = registration_payload("linear", launch_connector_tools("linear"))
    elif template == "crm-guardrail":
        base["tools"] = [
            {
                "name": "crm_update_customer",
                "capability_card": {
                    "name": "CRM Update Customer",
                    "description": "Update a customer record after entity resolution and policy checks.",
                    "capability": "crm.customer.update",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "customer_id": {"type": "string"},
                            "account_status": {"type": "string"},
                            "note": {"type": "string"},
                        },
                        "required": ["customer_id"],
                    },
                    "idempotency": {"mode": "caller_provided", "key_fields": ["customer_id", "account_status"]},
                    "retry": {"safe": False, "max_attempts": 1, "backoff_ms": 250},
                    "tags": ["crm", "customer", "write"],
                },
                "mcp_tool": "crm_update_customer",
                "approval_required": True,
                "risk_level": "high",
            }
        ]
    else:
        base["tools"] = [
            {
                "name": "system_status",
                "capability_card": {
                    "name": "System Status",
                    "description": "Read current system status before an agent takes action.",
                    "capability": "system.status.read",
                    "input_schema": {"type": "object", "properties": {}},
                    "idempotency": {"mode": "automatic", "key_fields": []},
                    "retry": {"safe": True, "max_attempts": 2, "backoff_ms": 250},
                    "tags": ["status", "read"],
                },
                "mcp_tool": "system_status",
                "approval_required": False,
                "risk_level": "low",
            }
        ]
    return base


def sample_agent_source(name: str) -> str:
    return textwrap.dedent(
        f"""\
        import {{ Invoke }} from "./sdk";

        const invoke = Invoke.fromEnv();

        async function main() {{
          const result = await invoke.call({{
            tool: "system.status",
            params: {{}},
            agentId: "{slugify(name)}",
          }});

          console.log(JSON.stringify(result, null, 2));
        }}

        main().catch((error) => {{
          console.error(error);
          process.exit(1);
        }});
        """
    )


def sample_sdk_source() -> str:
    return textwrap.dedent(
        """\
        export type InvokeCall = {
          tool: string;
          params: Record<string, unknown>;
          agentId?: string;
          idempotencyKey?: string;
        };

        export class Invoke {
          constructor(
            private readonly options: {
              baseUrl: string;
              apiKey: string;
            },
          ) {}

          static fromEnv() {
            const baseUrl = process.env.INVOKE_BASE_URL ?? "http://localhost:8000";
            const apiKey = process.env.INVOKE_API_KEY;
            if (!apiKey) throw new Error("Set INVOKE_API_KEY");
            return new Invoke({ baseUrl, apiKey });
          }

          async call(input: InvokeCall) {
            const response = await fetch(`${this.options.baseUrl.replace(/\\/+$/, "")}/v1/call`, {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                "X-API-Key": this.options.apiKey,
              },
              body: JSON.stringify({
                tool: input.tool,
                params: input.params,
                agent_id: input.agentId ?? "default_agent",
                ...(input.idempotencyKey ? { idempotency_key: input.idempotencyKey } : {}),
              }),
            });

            if (!response.ok) {
              throw new Error(`Invoke error ${response.status}: ${await response.text()}`);
            }
            return response.json();
          }
        }
        """
    )


def pin_workspace_to_project(root: Path, workspace_id: str) -> None:
    """Record the workspace id in the project's invoke.json so the project is self-describing."""
    config_path = root / "invoke.json"
    if not config_path.exists():
        return
    try:
        config = load_json_file(config_path, {})
    except ValueError:
        return
    if isinstance(config, dict):
        config["workspace_id"] = workspace_id
        write_json_file(config_path, config)


def provision_workspace(args: argparse.Namespace, project_name: str, root: Path) -> str | None:
    """Provision a cloud runtime workspace for a freshly scaffolded project.

    Best-effort: prints the reason and returns None (without failing `init`) when the
    step is skipped or the runtime is unreachable, so scaffolding still succeeds offline.
    """
    if getattr(args, "no_cloud", False):
        print("↷ Skipped cloud workspace (--no-cloud)")
        return None

    credentials = load_credentials()
    base_url = getattr(args, "base_url", None) or credentials["base_url"]
    api_key = getattr(args, "api_key", None) or credentials["api_key"]
    if not api_key:
        print("↷ No API key found — skipped cloud workspace.")
        print("  Run `invoke login --api-key <key>`, then `invoke init` again (or `invoke deploy`).")
        return None

    workspace_name = getattr(args, "workspace_name", None) or project_name
    try:
        response = api_request("POST", base_url, "/v1/runtime/workspaces", api_key, {"name": workspace_name})
    except RuntimeError as exc:
        print(f"↷ Could not provision cloud workspace: {exc}")
        return None

    workspace = response.get("workspace") if isinstance(response, dict) else None
    workspace_id = (workspace or {}).get("id")
    if not workspace_id:
        print("↷ Runtime did not return a workspace id — skipped.")
        return None

    set_active_workspace(workspace_id)
    pin_workspace_to_project(root, workspace_id)
    print(f'✔ Initialized workspace "{workspace_name}" ({workspace_id})')
    print(f"✔ Connected to cloud {base_url.rstrip('/')}")
    return workspace_id


def write_local_runtime(root: Path, workspace_id: str | None, base_url: str) -> None:
    """Drop a local runtime marker so the project knows its workspace/runtime without the cloud."""
    write_json_file(
        root / ".invoke" / "runtime.json",
        {
            "workspace_id": workspace_id,
            "base_url": base_url.rstrip("/"),
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
    )


def init_command(args: argparse.Namespace) -> int:
    root = Path(args.name)
    project_name = root.name

    if args.template in {"claude-agent", "gemini-agent"}:
        from invoke.deploy import copy_template

        template_dir = "claude-agent-sdk" if args.template == "claude-agent" else "gemini-agent"
        label = "Claude Agent SDK" if args.template == "claude-agent" else "Gemini agent"
        path = copy_template(root, template=template_dir, force=args.force)
        print(f"✔ Created {label} project at {path}")
        workspace_id = provision_workspace(args, project_name, root)
        credentials = load_credentials()
        write_local_runtime(root, workspace_id, getattr(args, "base_url", None) or credentials["base_url"])
        print("✔ Created local runtime")
        print("\nNext:")
        print(f"  cd {path}")
        print("  npm install")
        print("  invoke deploy --dry-run")
        return 0

    if root.exists() and any(root.iterdir()) and not args.force:
        raise ValueError(f"{root} already exists and is not empty. Pass --force to write into it.")
    root.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(exist_ok=True)

    config = project_template(project_name, args.template)
    write_json_file(root / "invoke.json", config)
    (root / "src" / "index.ts").write_text(sample_agent_source(project_name), encoding="utf-8")
    (root / "src" / "sdk.ts").write_text(sample_sdk_source(), encoding="utf-8")
    (root / "README.md").write_text(
        textwrap.dedent(
            f"""\
            # {project_name}

            Invoke project scaffolded with the `{args.template}` template.

            ```bash
            invoke deploy
            invoke call system.status '{{}}'
            ```

            Edit `invoke.json` to add production tools or point `mcp_url` at your hosted MCP server.
            """
        ),
        encoding="utf-8",
    )
    print(f"✔ Created invoke.json ({root})")
    workspace_id = provision_workspace(args, project_name, root)
    credentials = load_credentials()
    write_local_runtime(root, workspace_id, getattr(args, "base_url", None) or credentials["base_url"])
    print("✔ Created local runtime")
    print("\nNext:")
    print(f"  cd {root}")
    print("  invoke deploy --dry-run")
    return 0


def read_project(root: Path) -> dict[str, Any]:
    config_path = root / "invoke.json"
    if not config_path.exists():
        raise ValueError(f"No invoke.json found in {root}. Run `invoke init <name>` first.")
    config = load_json_file(config_path, {})
    if not isinstance(config, dict):
        raise ValueError("invoke.json must contain an object")
    tools = config.get("tools")
    if tools is None and (root / "invoke.register.json").exists():
        tools = load_json_file(root / "invoke.register.json", [])
    if not isinstance(tools, list) or not tools:
        raise ValueError("Project must define tools in invoke.json or invoke.register.json")
    config["tools"] = tools
    return config


def read_project_config(root: Path) -> dict[str, Any]:
    config_path = root / "invoke.json"
    if not config_path.exists():
        return {}
    config = load_json_file(config_path, {})
    if not isinstance(config, dict):
        raise ValueError("invoke.json must contain an object")
    return config


def is_invoke_source_repo(root: Path) -> bool:
    package_path = root / "package.json"
    package = load_json_file(package_path, {}) if package_path.exists() else {}
    package_name = str(package.get("name") or "") if isinstance(package, dict) else ""
    source_markers = (
        root / "agentify.py",
        root / "main.py",
        root / "registry.py",
        root / "invoke",
    )
    return package_name == "@invokehq/cli" and all(marker.exists() for marker in source_markers)


def is_agent_project(root: Path, config: dict[str, Any]) -> bool:
    if is_invoke_source_repo(root):
        return False
    runtime = str(config.get("runtime") or "").lower()
    agent_type = str(config.get("agent_type") or config.get("type") or "").lower()
    has_entrypoint = bool(config.get("entrypoint")) or any(
        (root / candidate).exists()
        for candidate in (
            "src/index.ts",
            "src/index.js",
            "index.ts",
            "index.js",
            "agent.py",
            "src/agent.py",
            "main.py",
            "src/main.py",
        )
    )
    tools = config.get("tools")
    empty_tools = tools is None or tools == []
    return has_entrypoint and (
        runtime in {"node", "python", "claude-agent-sdk", "gemini-agent"}
        or agent_type in {"agent", "claude-agent", "claude-agent-sdk", "gemini-agent"}
        or empty_tools
    )


def discover_deploy_candidates(root: Path) -> tuple[list[Path], list[Path]]:
    agent_candidates: list[Path] = []
    project_candidates: list[Path] = []
    if not root.exists() or not root.is_dir():
        return agent_candidates, project_candidates
    ignored = {".git", "node_modules", ".next", "dist", "venv", ".venv", "__pycache__"}
    for child in sorted(item for item in root.iterdir() if item.is_dir() and item.name not in ignored):
        try:
            config = read_project_config(child)
        except Exception:
            config = {}
        if is_agent_project(child, config):
            agent_candidates.append(child)
        elif (child / "invoke.json").exists():
            project_candidates.append(child)
    return agent_candidates, project_candidates


def missing_project_error(root: Path) -> CliUsageError:
    return CliUsageError(
        f"No invoke.json or agent entrypoint found in {root}.\n\n"
        "Start a deployable agent:\n"
        "  invoke init my-gemini-agent --template gemini-agent\n"
        "  cd my-gemini-agent\n"
        "  invoke deploy\n\n"
        "Or deploy an existing agent project:\n"
        "  invoke deploy ./my-gemini-agent\n\n"
        "Invoke deploy will package the agent for Modal, inject tracing, and start Onyx supervision."
    )


def source_repo_deploy_error(root: Path, candidates: list[Path]) -> CliUsageError:
    candidate_lines = ""
    if candidates:
        choices = []
        for candidate in candidates[:8]:
            try:
                display = candidate.relative_to(root)
            except ValueError:
                display = candidate
            choices.append(f"  invoke deploy ./{display}")
        candidate_lines = "\n\nDeploy one of the agent projects in this repo:\n" + "\n".join(choices)
    return CliUsageError(
        "You are in the Invoke CLI/backend source repo, not an agent project.\n\n"
        "Start a deployable agent:\n"
        "  invoke init my-gemini-agent --template gemini-agent\n"
        "  cd my-gemini-agent\n"
        "  invoke deploy\n\n"
        "Or deploy an existing agent project:\n"
        "  invoke deploy ./my-gemini-agent"
        f"{candidate_lines}\n\n"
        "This guard prevents accidentally deploying the Invoke backend as your agent."
    )


def project_mcp_url(root: Path, config: dict[str, Any], explicit_mcp_url: str | None = None) -> str | None:
    if explicit_mcp_url:
        return explicit_mcp_url
    configured = config.get("mcp_url")
    if not is_placeholder_mcp_url(configured):
        return configured
    dev_info = load_json_file(dev_runtime_path(root), {})
    if isinstance(dev_info, dict) and dev_info.get("mcp_url"):
        return str(dev_info["mcp_url"])
    return configured


def registration_tool_name(tool: dict[str, Any]) -> str:
    return str(tool.get("mcp_tool") or tool.get("name") or "tool")


def registration_tool_description(tool: dict[str, Any]) -> str:
    card = tool.get("capability_card") if isinstance(tool.get("capability_card"), dict) else {}
    return str(card.get("description") or tool.get("description") or registration_tool_name(tool))


def registration_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    card = tool.get("capability_card") if isinstance(tool.get("capability_card"), dict) else {}
    schema = card.get("input_schema") or tool.get("input_schema") or {"type": "object", "properties": {}}
    return schema if isinstance(schema, dict) else {"type": "object", "properties": {}}


def jsonrpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def make_dev_handler(config: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    tools = {registration_tool_name(tool): tool for tool in config["tools"]}
    project_name = str(config.get("name") or "invoke-dev")

    class InvokeDevHandler(BaseHTTPRequestHandler):
        server_version = "InvokeDev/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[invoke dev] {self.address_string()} - {fmt % args}")

        def send_json(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/health":
                self.send_json(200, {"status": "ok", "project": project_name})
                return
            self.send_json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            if self.path != "/mcp":
                self.send_json(404, {"error": "not_found"})
                return
            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except Exception as exc:
                self.send_json(400, jsonrpc_error(None, -32700, f"Invalid JSON: {exc}"))
                return

            request_id = payload.get("id")
            method = payload.get("method")
            if method == "initialize":
                self.send_json(
                    200,
                    jsonrpc_result(
                        request_id,
                        {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": project_name, "version": "0.1.0"},
                        },
                    ),
                )
                return
            if method == "notifications/initialized":
                self.send_json(202, {})
                return
            if method == "tools/list":
                self.send_json(
                    200,
                    jsonrpc_result(
                        request_id,
                        {
                            "tools": [
                                {
                                    "name": name,
                                    "description": registration_tool_description(tool),
                                    "inputSchema": registration_tool_schema(tool),
                                }
                                for name, tool in tools.items()
                            ]
                        },
                    ),
                )
                return
            if method == "tools/call":
                params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
                name = params.get("name")
                arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
                if name not in tools:
                    self.send_json(200, jsonrpc_error(request_id, -32601, f"Unknown tool {name!r}"))
                    return
                self.send_json(
                    200,
                    jsonrpc_result(
                        request_id,
                        {
                            "ok": True,
                            "tool": name,
                            "arguments": arguments,
                            "mode": "invoke_dev_mock",
                            "message": f"{name} handled by local invoke dev server",
                        },
                    ),
                )
                return
            self.send_json(200, jsonrpc_error(request_id, -32601, f"Unsupported method {method}"))

    return InvokeDevHandler


def deployment_record(project_root: Path, provider: dict[str, Any], tools: list[dict[str, Any]], base_url: str) -> dict[str, Any]:
    return {
        "project": str(project_root.resolve()),
        "name": provider.get("name"),
        "provider_id": provider.get("id"),
        "slug": provider.get("slug"),
        "gateway_url": provider.get("gateway_url"),
        "base_url": base_url,
        "tools": [tool.get("id") or tool.get("key") or tool.get("name") for tool in tools],
        "deployed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def save_deployment(record: dict[str, Any]) -> None:
    deployments = load_json_file(deployments_path(), [])
    if not isinstance(deployments, list):
        deployments = []
    deployments = [item for item in deployments if item.get("project") != record["project"]]
    deployments.append(record)
    write_json_file(deployments_path(), deployments)


def deploy_command(args: argparse.Namespace) -> int:
    root = Path(args.path)
    if is_invoke_source_repo(root):
        agent_candidates, project_candidates = discover_deploy_candidates(root)
        raise source_repo_deploy_error(root, agent_candidates or project_candidates)

    raw_config = read_project_config(root)
    if not raw_config and not is_agent_project(root, raw_config) and not (root / "invoke.json").exists():
        agent_candidates, project_candidates = discover_deploy_candidates(root)
        candidates = agent_candidates or project_candidates
        if len(candidates) == 1:
            root = candidates[0]
            raw_config = read_project_config(root)
            kind = "agent project" if agent_candidates else "project"
            print(f"Found Invoke {kind}: {root}")
        elif len(candidates) > 1:
            relative = "\n".join(f"  invoke deploy {candidate}" for candidate in candidates[:8])
            raise CliUsageError(f"Multiple deployable projects found. Choose one:\n{relative}")
        else:
            raise missing_project_error(root)

    if is_agent_project(root, raw_config):
        from dataclasses import asdict

        from invoke.deploy import deploy_claude_agent

        result = deploy_claude_agent(
            root,
            app_name=args.slug or raw_config.get("slug") or raw_config.get("name"),
            dry_run=args.dry_run,
        )
        record = {
            "project": str(root.resolve()),
            "name": result.plan.app_name,
            "provider_id": result.deployment_id,
            "slug": result.plan.app_name,
            "gateway_url": result.endpoint_url,
            "dashboard_url": result.dashboard_url,
            "base_url": "modal",
            "tools": ["agent.run"],
            "deployed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "mode": "modal_agent",
            "trace_path": result.trace_path,
        }
        if not args.dry_run:
            save_deployment(record)

        from invoke.deploy import sanitized_result

        payload = {"success": result.success, "mode": "modal_agent", **sanitized_result(result)}
        print(json.dumps(payload, indent=2))
        if not args.dry_run:
            print("\nInvoke deploy complete.")
            print(f"Endpoint: {result.endpoint_url or '(Modal endpoint pending; check dashboard)'}")
            print(f"Dashboard: {result.dashboard_url}")
            print(f"Traces: {result.trace_path}")
            print("Onyx: watching new traces immediately.")
        return 0

    config = read_project(root)
    tools = config["tools"]
    mcp_url = project_mcp_url(root, config, args.mcp_url)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "success": True,
                    "dry_run": True,
                    "project": str(root.resolve()),
                    "name": config.get("name") or root.name,
                    "tools": [tool.get("name") for tool in tools],
                    "mcp_url": mcp_url,
                },
                indent=2,
            )
        )
        return 0

    credentials = require_credentials(args)
    provider_body = {
        "name": config.get("name") or root.name,
        "owner_email": args.owner_email or config.get("owner_email") or "dev@example.com",
        "slug": args.slug or config.get("slug"),
    }
    provider_response = api_request("POST", credentials["base_url"], "/providers", credentials["api_key"], provider_body)
    provider = provider_response.get("provider") or {}
    provider_id = provider.get("id")
    if not provider_id:
        raise RuntimeError(f"Provider creation returned no provider id: {provider_response}")

    created_tools = []
    for tool in tools:
        payload = dict(tool)
        if not payload.get("mcp_url") and mcp_url:
            payload["mcp_url"] = mcp_url
        created = api_request("POST", credentials["base_url"], f"/providers/{provider_id}/tools", credentials["api_key"], payload)
        created_tools.append(created.get("tool") or created)

    record = deployment_record(root, provider, created_tools, credentials["base_url"])
    save_deployment(record)
    print(json.dumps({"success": True, "provider": provider, "tools": created_tools}, indent=2))
    return 0


def agents_list_command(args: argparse.Namespace) -> int:
    deployments = load_json_file(deployments_path(), [])
    if not deployments:
        print("No Invoke projects deployed from this machine yet.")
        return 0
    if args.json:
        print(json.dumps({"success": True, "agents": deployments}, indent=2))
        return 0

    print("NAME\tSLUG\tTOOLS\tBASE URL")
    for item in deployments:
        print(
            f"{item.get('name') or '-'}\t"
            f"{item.get('slug') or '-'}\t"
            f"{len(item.get('tools') or [])}\t"
            f"{item.get('base_url') or '-'}"
        )
    return 0


def tools_command(args: argparse.Namespace) -> int:
    credentials = require_credentials(args)
    query = f"?q={urllib.parse.quote(args.query)}" if args.query else ""
    response = api_request("GET", credentials["base_url"], f"/v1/tools{query}", credentials["api_key"])
    tools = response.get("tools") if isinstance(response.get("tools"), list) else []
    if args.json:
        print(json.dumps(response, indent=2))
        return 0

    if not tools:
        print("No tools available for this API key.")
        print("Try `invoke deploy` from a project with invoke.json, or check your token scopes.")
        return 0

    print("TOOL\tRISK\tAPPROVAL\tCONFIGURED\tDESCRIPTION")
    for tool in tools:
        tool_id = tool.get("id") or tool.get("key") or tool.get("name") or "-"
        risk = tool.get("risk_level") or "-"
        approval = "yes" if tool.get("approval_required") else "no"
        configured = "yes" if tool.get("configured", True) else "no"
        description = str(tool.get("description") or "").replace("\n", " ")
        print(f"{tool_id}\t{risk}\t{approval}\t{configured}\t{description}")
    return 0


def call_command(args: argparse.Namespace) -> int:
    if not args.tool:
        raise CliUsageError(
            "Missing tool.\n"
            "Usage: invoke call <tool> '<json_params>'\n"
            "Example: invoke call linear.create_issue '{\"team_id\":\"ENG\",\"title\":\"Fix retry\"}'"
        )
    credentials = require_credentials(args)
    try:
        params = json.loads(args.params)
    except json.JSONDecodeError as exc:
        raise ValueError(f"params must be valid JSON: {exc}") from exc
    response = api_request(
        "POST",
        credentials["base_url"],
        "/v1/call",
        credentials["api_key"],
        {
            "tool": args.tool,
            "params": params,
            "agent_id": args.agent_id,
            **({"idempotency_key": args.idempotency_key} if args.idempotency_key else {}),
        },
    )
    print(json.dumps(response, indent=2))
    return 0


def approvals_command(args: argparse.Namespace) -> int:
    credentials = require_credentials(args)
    if args.approvals_action == "approve":
        if not args.approval_id:
            raise CliUsageError("Usage: invoke approvals approve <approval_id>")
        response = api_request(
            "POST",
            credentials["base_url"],
            f"/v1/approvals/{args.approval_id}/approve",
            credentials["api_key"],
            {"reviewed_by": args.reviewed_by},
        )
        print(json.dumps(response, indent=2))
        return 0
    if args.approvals_action == "reject":
        if not args.approval_id:
            raise CliUsageError("Usage: invoke approvals reject <approval_id>")
        response = api_request(
            "POST",
            credentials["base_url"],
            f"/v1/approvals/{args.approval_id}/reject",
            credentials["api_key"],
            {"reviewed_by": args.reviewed_by},
        )
        print(json.dumps(response, indent=2))
        return 0

    response = api_request("GET", credentials["base_url"], "/v1/approvals", credentials["api_key"])
    approvals = response.get("approvals") if isinstance(response.get("approvals"), list) else []
    if args.json:
        print(json.dumps(response, indent=2))
        return 0
    if not approvals:
        print("No pending approvals.")
        return 0
    print("ID\tTOOL\tAGENT\tRISK\tSTATUS")
    for approval in approvals:
        print(
            f"{approval.get('id') or '-'}\t"
            f"{approval.get('tool') or '-'}\t"
            f"{approval.get('agent_id') or '-'}\t"
            f"{approval.get('risk_level') or '-'}\t"
            f"{approval.get('status') or '-'}"
        )
    return 0


def search_command(args: argparse.Namespace) -> int:
    credentials = require_credentials(args)
    response = api_request(
        "POST",
        credentials["base_url"],
        "/v1/search",
        credentials["api_key"],
        {
            "query": args.query,
            "limit": args.limit,
        },
    )
    if args.json:
        print(json.dumps(response, indent=2))
        return 0

    print(f"Invoke search: {response.get('query') or args.query}")
    print(f"Provider: {response.get('provider', 'exa')}  Results: {len(response.get('results') or [])}")
    print()
    for index, result in enumerate(response.get("results") or [], start=1):
        title = result.get("title") or "Untitled result"
        summary = result.get("summary") or ""
        url = result.get("url") or ""
        print(f"{index}. {title}")
        if summary:
            print(textwrap.fill(summary, width=88, initial_indent="   ", subsequent_indent="   "))
        if url:
            print(f"   {url}")
        print()
    return 0


def action_payload_from_args(args: argparse.Namespace) -> dict[str, Any]:
    if not args.action:
        raise CliUsageError(
            f"Missing action.\n"
            f"Usage: invoke {args.command} <action> '<json_params>'\n"
            f"Example: invoke {args.command} stripe.charge_customer '{{\"customer_id\":\"cust_123\",\"amount\":2400}}'"
        )
    try:
        params = json.loads(args.params)
    except json.JSONDecodeError as exc:
        raise ValueError(f"params must be valid JSON: {exc}") from exc
    if not isinstance(params, dict):
        raise ValueError("params must be a JSON object")

    body: dict[str, Any] = {
        "action": args.action,
        "params": params,
        "agent_id": args.agent_id,
    }
    if args.idempotency_key:
        body["idempotency_key"] = args.idempotency_key
    if args.apply_safe_fixes:
        body["apply_safe_fixes"] = True
    if getattr(args, "preflight_only", False):
        body["preflight_only"] = True
    return body


def print_preflight_summary(preflight: dict[str, Any]) -> None:
    print(f"Risk: {preflight.get('risk_score', '-')}" + (
        f" ({preflight.get('risk_score_numeric')})" if preflight.get("risk_score_numeric") is not None else ""
    ))
    subject = preflight.get("subject") if isinstance(preflight.get("subject"), dict) else {}
    if subject.get("tool"):
        print(f"Subject: {subject.get('tool')} [{subject.get('safety_class') or 'unknown'}]")
    simulation = preflight.get("simulation") if isinstance(preflight.get("simulation"), dict) else {}
    if simulation:
        print(
            f"Simulation: {simulation.get('similar_traces', 0)} similar traces "
            f"via {simulation.get('source', 'patterns')}"
        )

    warnings = preflight.get("warnings") if isinstance(preflight.get("warnings"), list) else []
    if warnings:
        print("\nWarnings")
        for warning in warnings:
            print(f"- {warning.get('severity', 'warn')}: {warning.get('message')}")

    guardrails = preflight.get("recommended_guardrails") if isinstance(preflight.get("recommended_guardrails"), list) else []
    if guardrails:
        print("\nRecommended guardrails")
        for guardrail in guardrails:
            applied = "applied" if guardrail.get("applied") else "pending"
            print(f"- {guardrail.get('type')}: {guardrail.get('action')} [{applied}]")

    safe_fixes = preflight.get("safe_fixes") if isinstance(preflight.get("safe_fixes"), dict) else {}
    if safe_fixes.get("idempotency_key"):
        print(f"\nIdempotency key: {safe_fixes.get('idempotency_key')}")
    print(f"\nDecision: {preflight.get('decision', '-')}")


def preflight_command(args: argparse.Namespace) -> int:
    credentials = require_credentials(args)
    response = api_request(
        "POST",
        credentials["base_url"],
        "/v1/preflight",
        credentials["api_key"],
        action_payload_from_args(args),
    )
    if args.json:
        print(json.dumps(response, indent=2))
        return 0
    print(f"Invoke preflight: {args.action}")
    print_preflight_summary(response.get("preflight") or {})
    return 0


def execute_command(args: argparse.Namespace) -> int:
    credentials = require_credentials(args)
    response = api_request(
        "POST",
        credentials["base_url"],
        "/v1/executions",
        credentials["api_key"],
        action_payload_from_args(args),
    )
    if args.json:
        print(json.dumps(response, indent=2))
        return 0

    execution = response.get("execution") if isinstance(response.get("execution"), dict) else {}
    certificate = response.get("certificate") if isinstance(response.get("certificate"), dict) else {}
    print(f"Invoke execute: {args.action}")
    preflight = response.get("preflight") if isinstance(response.get("preflight"), dict) else {}
    if preflight:
        print_preflight_summary(preflight)
        print()
    print(f"Execution ID: {certificate.get('execution_id') or execution.get('execution_id') or '-'}")
    print(f"Decision: {certificate.get('decision') or execution.get('decision') or execution.get('status') or '-'}")
    print(f"Final outcome: {certificate.get('final_outcome') or execution.get('final_outcome') or '-'}")
    print("Certificate returned: yes")
    return 0


def workflow_command(args: argparse.Namespace) -> int:
    credentials = require_credentials(args)
    body: dict[str, Any] = {}
    if args.query:
        body["query"] = args.query
    if args.limit:
        body["limit"] = args.limit
    if args.params:
        try:
            body["params"] = json.loads(args.params)
        except json.JSONDecodeError as exc:
            raise ValueError(f"params must be valid JSON: {exc}") from exc

    response = api_request(
        "POST",
        credentials["base_url"],
        f"/v1/workflows/{args.workflow}/run",
        credentials["api_key"],
        body,
    )
    if args.json:
        print(json.dumps(response, indent=2))
        return 0

    print(response.get("summary", f"Workflow {args.workflow} completed."))
    print()
    for event in response.get("trace") or []:
        step = event.get("step", "step")
        title = event.get("title", "")
        status = event.get("status", "")
        detail = event.get("detail", "")
        print(f"- {step}: {title} [{status}]")
        if detail:
            print(textwrap.fill(detail, width=88, initial_indent="  ", subsequent_indent="  "))
    return 0


def dev_command(args: argparse.Namespace) -> int:
    root = Path(args.path)
    config = read_project(root)
    host = args.host
    port = args.port
    mcp_url = f"http://{host if host not in {'0.0.0.0', '::'} else 'localhost'}:{port}/mcp"
    write_json_file(
        dev_runtime_path(root),
        {
            "mcp_url": mcp_url,
            "host": host,
            "port": port,
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
    )

    if args.dry_run:
        print(json.dumps({"success": True, "mcp_url": mcp_url, "tools": [registration_tool_name(tool) for tool in config["tools"]]}, indent=2))
        return 0

    server = HTTPServer((host, port), make_dev_handler(config))
    print(f"Invoke dev MCP server running at {mcp_url}")
    print(f"Project: {root.resolve()}")
    print("In another terminal, run:")
    print("  invoke deploy")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping invoke dev server.")
    finally:
        server.server_close()
    return 0


# The five layers Invoke is built from. Every command maps to exactly one, so the
# tool reads as one system rather than a bag of subcommands.
LAYERS: list[tuple[str, str]] = [
    ("Identity", "who each agent is, what it may do, what it may spend"),
    ("Context", "one governed source of truth every agent shares, with provenance"),
    ("Coordination", "agent-to-agent handoffs as a first-class, durable primitive"),
    ("Execution", "every real action governed like a production transaction: exactly-once, authorized, reconciled, approved if risky, receipted"),
    ("Observability", "who did what, why, at what cost, and where the bottlenecks are"),
]

# command -> (layer, one-line summary), used for the layered help view and `invoke layers`.
COMMAND_LAYERS: dict[str, tuple[str, str]] = {
    "login": ("Identity", "Authenticate and save runtime credentials"),
    "auth": ("Identity", "Authenticate (alias for login)"),
    "config": ("Identity", "Show or update CLI config"),
    "agents": ("Identity", "List agents / projects deployed from this machine"),
    "search": ("Context", "Search live docs/context through Invoke"),
    "workflow": ("Coordination", "Run a packaged multi-step workflow"),
    "init": ("Execution", "Scaffold a project and provision a workspace"),
    "deploy": ("Execution", "Deploy runtime, policies, agents, tools"),
    "run": ("Execution", "Run an agent or a workflow file"),
    "call": ("Execution", "Call a tool through Invoke"),
    "execute": ("Execution", "Execute an action through the control boundary"),
    "preflight": ("Execution", "Simulate an action before execution"),
    "approvals": ("Execution", "List or resolve pending approvals"),
    "wrap": ("Execution", "Generate a tool wrapper project"),
    "dev": ("Execution", "Run a local MCP server for this project"),
    "tools": ("Execution", "List available tools"),
    "status": ("Observability", "Runtime health and workspace dashboard"),
    "logs": ("Observability", "Show or follow the execution event log"),
    "doctor": ("Observability", "Diagnose credentials and runtime health"),
    "layers": ("Observability", "Explain the five Invoke layers"),
}


def commands_for_layer(layer: str) -> list[tuple[str, str]]:
    return [(name, meta[1]) for name, meta in COMMAND_LAYERS.items() if meta[0] == layer]


def layered_epilog() -> str:
    lines = ["commands, by layer:"]
    for name, definition in LAYERS:
        lines.append("")
        lines.append(f"  {name} — {definition}")
        for cmd, summary in commands_for_layer(name):
            lines.append(f"    {cmd:<10} {summary}")
    lines.append("")
    lines.append("Run `invoke layers` for the model, or `invoke <command> --help` for a command.")
    return "\n".join(lines)


def layers_command(args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        payload = {
            "layers": [
                {"name": name, "definition": definition,
                 "commands": [cmd for cmd, _ in commands_for_layer(name)]}
                for name, definition in LAYERS
            ]
        }
        print(json.dumps(payload, indent=2))
        return 0
    print("Invoke is one system with five layers. Every command maps to one:\n")
    for index, (name, definition) in enumerate(LAYERS, start=1):
        print(f"{index}. {name}")
        print(f"   {definition}")
        cmds = commands_for_layer(name)
        if cmds:
            print(f"   commands: {', '.join(cmd for cmd, _ in cmds)}")
        print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="invoke",
        description="Execution reliability infrastructure for AI agents.",
        epilog=layered_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {package_version()}")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    login = subparsers.add_parser("login", help="Save Invoke runtime credentials.")
    login.add_argument("--base-url", default=DEFAULT_API_URL, help="Invoke runtime URL.")
    login.add_argument("--api-key", help="Invoke API key. If omitted, prompts interactively.")
    login.set_defaults(func=login_command)

    status = subparsers.add_parser("status", help="Show login and project context.")
    status.add_argument("--base-url", help="Override Invoke runtime URL.")
    status.add_argument("--api-key", help="Override Invoke API key.")
    status.add_argument("--check", action="store_true", help="Call /health and verify the runtime is reachable.")
    status.add_argument("--workspace", help="Workspace id to inspect.")
    status.add_argument("--plain", action="store_true", help="Show CLI/login context instead of the runtime dashboard.")
    status.add_argument("--json", action="store_true", help="Print the raw status payload as JSON.")
    status.set_defaults(func=status_command)

    doctor = subparsers.add_parser("doctor", help="Check credentials and runtime health.")
    doctor.add_argument("--base-url", help="Override Invoke runtime URL.")
    doctor.add_argument("--api-key", help="Override Invoke API key.")
    doctor.set_defaults(func=doctor_command)

    config = subparsers.add_parser("config", help="Show or update CLI config.")
    config.add_argument("key", nargs="?", help="Config key to read or update: base-url or api-key.")
    config.add_argument("value", nargs="?", help="New config value.")
    config.add_argument("--json", action="store_true", help="Print config as JSON with the API key masked.")
    config.set_defaults(func=config_command)

    init = subparsers.add_parser("init", help="Scaffold an Invoke project.")
    init.add_argument("name", help="Project directory/name.")
    init.add_argument(
        "--template",
        choices=["gemini-agent", "claude-agent", "default", "linear", "crm-guardrail"],
        default="gemini-agent",
    )
    init.add_argument("--force", action="store_true", help="Write into an existing non-empty directory.")
    init.add_argument("--no-cloud", action="store_true", help="Skip provisioning a cloud runtime workspace.")
    init.add_argument("--workspace-name", help="Name for the provisioned workspace (defaults to the project name).")
    init.add_argument("--base-url", help="Override Invoke runtime URL.")
    init.add_argument("--api-key", help="Override Invoke API key.")
    init.set_defaults(func=init_command)

    deploy = subparsers.add_parser("deploy", help="Register this project with an Invoke runtime.")
    deploy.add_argument("path", nargs="?", default=".", help="Project directory containing invoke.json.")
    deploy.add_argument("--base-url", help="Override Invoke runtime URL.")
    deploy.add_argument("--api-key", help="Override Invoke API key.")
    deploy.add_argument("--owner-email", help="Provider owner email.")
    deploy.add_argument("--slug", help="Provider slug.")
    deploy.add_argument("--mcp-url", help="Hosted MCP URL for registered tools.")
    deploy.add_argument("--dry-run", action="store_true", help="Validate and print the deployment plan without calling the API.")
    deploy.set_defaults(func=deploy_command)

    dev = subparsers.add_parser("dev", help="Run a local MCP server for this Invoke project.")
    dev.add_argument("path", nargs="?", default=".", help="Project directory containing invoke.json.")
    dev.add_argument("--host", default="127.0.0.1")
    dev.add_argument("--port", type=int, default=8787)
    dev.add_argument("--dry-run", action="store_true", help="Print the local MCP URL without starting the server.")
    dev.set_defaults(func=dev_command)

    tools = subparsers.add_parser("tools", help="List available tools.")
    tools.add_argument("query", nargs="?", help="Optional search query.")
    tools.add_argument("--json", action="store_true", help="Print the full JSON response.")
    tools.add_argument("--base-url", help="Override Invoke runtime URL.")
    tools.add_argument("--api-key", help="Override Invoke API key.")
    tools.set_defaults(func=tools_command)

    call = subparsers.add_parser("call", help="Call a tool through Invoke.")
    call.add_argument("tool", nargs="?", help="Tool id, for example linear.create_issue.")
    call.add_argument("params", nargs="?", default="{}", help="JSON params object.")
    call.add_argument("--agent-id", default="cli_agent")
    call.add_argument("--idempotency-key")
    call.add_argument("--base-url", help="Override Invoke runtime URL.")
    call.add_argument("--api-key", help="Override Invoke API key.")
    call.set_defaults(func=call_command)

    approvals = subparsers.add_parser("approvals", help="List or manage pending approvals.")
    approvals.add_argument(
        "approvals_action",
        nargs="?",
        choices=["list", "approve", "reject"],
        default="list",
        help="Action to run. Defaults to list.",
    )
    approvals.add_argument("approval_id", nargs="?", help="Approval id for approve/reject.")
    approvals.add_argument("--reviewed-by", default="cli", help="Reviewer name for approve/reject.")
    approvals.add_argument("--json", action="store_true", help="Print the full JSON response.")
    approvals.add_argument("--base-url", help="Override Invoke runtime URL.")
    approvals.add_argument("--api-key", help="Override Invoke API key.")
    approvals.set_defaults(func=approvals_command)

    search = subparsers.add_parser("search", help="Search live docs/context through Invoke.")
    search.add_argument("query", help="Search query, for example 'latest MCP agent failures'.")
    search.add_argument("--limit", type=int, default=5, help="Number of Exa results to return, 1-10.")
    search.add_argument("--json", action="store_true", help="Print the full JSON response.")
    search.add_argument("--base-url", help="Override Invoke runtime URL.")
    search.add_argument("--api-key", help="Override Invoke API key.")
    search.set_defaults(func=search_command)

    preflight = subparsers.add_parser("preflight", help="Simulate an agent action before execution.")
    preflight.add_argument("action", nargs="?", help="Action name, for example stripe.charge_customer.")
    preflight.add_argument("params", nargs="?", default="{}", help="JSON params object.")
    preflight.add_argument("--agent-id", default="cli_agent")
    preflight.add_argument("--idempotency-key")
    preflight.add_argument("--apply-safe-fixes", action="store_true", help="Apply recommended guardrails in the returned plan.")
    preflight.add_argument("--json", action="store_true", help="Print the full JSON response.")
    preflight.add_argument("--base-url", help="Override Invoke runtime URL.")
    preflight.add_argument("--api-key", help="Override Invoke API key.")
    preflight.set_defaults(func=preflight_command)

    execute = subparsers.add_parser("execute", help="Execute an agent action through Invoke's control boundary.")
    execute.add_argument("action", nargs="?", help="Action name, for example stripe.charge_customer.")
    execute.add_argument("params", nargs="?", default="{}", help="JSON params object.")
    execute.add_argument("--agent-id", default="cli_agent")
    execute.add_argument("--idempotency-key")
    execute.add_argument("--apply-safe-fixes", action="store_true", help="Apply recommended guardrails before returning the receipt.")
    execute.add_argument("--preflight-only", action="store_true", help="Only run the pre-execution simulation.")
    execute.add_argument("--json", action="store_true", help="Print the full JSON response.")
    execute.add_argument("--base-url", help="Override Invoke runtime URL.")
    execute.add_argument("--api-key", help="Override Invoke API key.")
    execute.set_defaults(func=execute_command)

    workflow = subparsers.add_parser("workflow", help="Run a packaged Invoke workflow.")
    workflow.add_argument(
        "workflow",
        choices=["safe-tool-execution", "live-context-retrieval", "failure-trace-visualization"],
        help="Workflow id to run.",
    )
    workflow.add_argument("--query", help="Live-context query for Exa-backed workflows.")
    workflow.add_argument("--limit", type=int, help="Search result limit for live-context workflows.")
    workflow.add_argument("--params", help="Optional JSON params for the workflow.")
    workflow.add_argument("--json", action="store_true", help="Print the full JSON response.")
    workflow.add_argument("--base-url", help="Override Invoke runtime URL.")
    workflow.add_argument("--api-key", help="Override Invoke API key.")
    workflow.set_defaults(func=workflow_command)

    agents = subparsers.add_parser("agents", help="Manage locally deployed Invoke projects.")
    agents_subparsers = agents.add_subparsers(dest="agents_command", required=True)
    agents_list = agents_subparsers.add_parser("list", help="List projects deployed from this machine.")
    agents_list.add_argument("--json", action="store_true", help="Print JSON.")
    agents_list.set_defaults(func=agents_list_command)

    wrap = subparsers.add_parser("wrap", help="Generate a wrapper project.")
    wrap.add_argument("target", help="postgresql, github, notion, linear, or a FastAPI/service name.")
    wrap.add_argument("--query", help="SQL query for postgresql wrappers.")
    wrap.add_argument("--database-url-env", default="DATABASE_URL", help="Env var used by generated PostgreSQL wrapper.")
    wrap.add_argument("--allow-write", action="store_true", help="Allow non-read-only PostgreSQL statements.")
    wrap.add_argument("--openapi", help="OpenAPI JSON file for FastAPI/service wrappers.")
    wrap.add_argument("--base-url", default="http://localhost:8000", help="Base URL for generated HTTP wrappers.")
    wrap.add_argument("--name", help="Human-friendly wrapper or tool name.")
    wrap.add_argument("--description", help="Capability description.")
    wrap.add_argument("--output", default="wrapped_tools", help="Output directory.")
    wrap.set_defaults(func=wrap_command)

    layers = subparsers.add_parser("layers", help="Explain the five Invoke layers.")
    layers.add_argument("--json", action="store_true", help="Print the layer model as JSON.")
    layers.set_defaults(func=layers_command)

    auth = subparsers.add_parser("auth", help="Authenticate (alias for login).")
    auth.add_argument("--base-url", default=DEFAULT_API_URL, help="Invoke runtime URL.")
    auth.add_argument("--api-key", help="Invoke API key. If omitted, prompts interactively.")
    auth.set_defaults(func=login_command)

    run = subparsers.add_parser("run", help="Run an agent or a workflow file.")
    run.add_argument("target", help="Agent name, or path to a workflow .yaml/.yml/.json file.")
    run.add_argument("--params", default="{}", help="JSON params/input for the run.")
    run.add_argument("--agent-id", default="cli_agent", help="Requesting agent id.")
    run.add_argument("--workspace", help="Workspace id to run in.")
    run.add_argument("--detach", action="store_true", help="Start the run and return without waiting.")
    run.add_argument("--json", action="store_true", help="Print the raw JSON response.")
    run.add_argument("--base-url", help="Override Invoke runtime URL.")
    run.add_argument("--api-key", help="Override Invoke API key.")
    run.set_defaults(func=run_command)

    logs = subparsers.add_parser("logs", help="Show or follow the execution event log.")
    logs.add_argument("--workspace", help="Workspace id to read.")
    logs.add_argument("-f", "--follow", action="store_true", help="Stream new events as they occur.")
    logs.add_argument("--limit", type=int, default=50, help="Max events to show (default 50).")
    logs.add_argument("--type", dest="types", help="Comma-separated event types to include.")
    logs.add_argument("--since", type=int, default=0, help="Only events after this sequence number.")
    logs.add_argument("--json", action="store_true", help="Print raw event JSON.")
    logs.add_argument("--base-url", help="Override Invoke runtime URL.")
    logs.add_argument("--api-key", help="Override Invoke API key.")
    logs.set_defaults(func=logs_command)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw_args = list(argv if argv is not None else sys.argv[1:])
    aliases = {
        "calls": "call",
        "tool": "tools",
        "ls": "tools",
    }
    if raw_args and raw_args[0] in aliases:
        raw_args[0] = aliases[raw_args[0]]
    args = parser.parse_args(raw_args)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"invoke: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
