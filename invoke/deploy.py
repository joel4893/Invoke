"""Modal-backed deployment planning for Claude Agent SDK projects.

This module is intentionally small and import-safe. The public CLI can call
`deploy_claude_agent(path)` without importing Modal unless the caller actually
wants to deploy. That lets the local CLI stay lightweight while giving us a
real place to evolve hosted agent deployment.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .onyx.analyzer import OnyxSuggestion, analyze_traces
from .supervisor.monitor import TraceStore, build_boot_trace


DEFAULT_ENTRYPOINTS = (
    "src/index.ts",
    "src/index.js",
    "index.ts",
    "index.js",
    "agent.py",
    "src/agent.py",
    "main.py",
    "src/main.py",
)
DEFAULT_MODAL_IMAGE = "invoke/agent-runtime:latest"


@dataclass
class DeployPlan:
    """A concrete deployment plan for one agent project."""

    project_root: str
    app_name: str
    entrypoint: str
    modal_volume: str
    modal_image: str = DEFAULT_MODAL_IMAGE
    env: dict[str, str] = field(default_factory=dict)
    endpoint_name: str = "invoke"
    tracing_enabled: bool = True
    onyx_enabled: bool = True
    onyx_mode: str = "auto"
    persistence: dict[str, str] = field(default_factory=dict)
    robustness: dict[str, bool] = field(default_factory=dict)


@dataclass
class DeployResult:
    """Result returned by a deploy attempt or dry run."""

    success: bool
    plan: DeployPlan
    deployment_id: str
    status: str
    message: str
    modal_app_name: str | None = None
    endpoint_url: str | None = None
    dashboard_url: str | None = None
    trace_path: str | None = None
    modal_source_path: str | None = None
    onyx_suggestions: list[OnyxSuggestion] = field(default_factory=list)


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return value[:2] + "..."
    return value[:8] + "..." + value[-4:]


def sanitized_result(result: DeployResult) -> dict[str, Any]:
    payload = asdict(result)
    env = payload.get("plan", {}).get("env")
    if isinstance(env, dict):
        payload["plan"]["env"] = {key: mask_secret(str(value)) for key, value in env.items()}
    return payload


def slugify(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)
    return "-".join(part for part in cleaned.split("-") if part) or "invoke-agent"


def modal_endpoint_label(app_slug: str) -> str:
    """Return a stable Modal web endpoint label scoped to this app.

    Modal endpoint labels become part of the public subdomain. A generic label
    like "invoke" collides across every deployed agent in the same workspace.
    """

    base = slugify(app_slug)
    suffix = "-invoke"
    max_label_length = 63
    if len(base) + len(suffix) > max_label_length:
        base = base[: max_label_length - len(suffix)].rstrip("-") or "agent"
    return f"{base}{suffix}"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_project_config(project_root: Path) -> dict[str, Any]:
    for name in ("invoke.json", "agent.json", "package.json"):
        path = project_root / name
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
            if isinstance(data, dict):
                return data
    return {}


def infer_entrypoint(project_root: Path, config: dict[str, Any]) -> str:
    configured = config.get("entrypoint") or config.get("main")
    candidates = (str(configured),) if configured else DEFAULT_ENTRYPOINTS
    for candidate in candidates:
        if candidate and (project_root / candidate).exists():
            return candidate
    raise FileNotFoundError(
        "Could not find an agent entrypoint. Expected one of: "
        + ", ".join(DEFAULT_ENTRYPOINTS)
        + ". You can also set `entrypoint` in invoke.json."
    )


def build_deploy_plan(project_path: str | Path, *, app_name: str | None = None) -> DeployPlan:
    project_root = Path(project_path).expanduser().resolve()
    if not project_root.exists() or not project_root.is_dir():
        raise FileNotFoundError(f"Agent project does not exist: {project_root}")

    config = load_project_config(project_root)
    inferred_name = app_name or config.get("name") or project_root.name
    slug = slugify(str(inferred_name))
    entrypoint = infer_entrypoint(project_root, config)

    env_keys = [
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "INVOKE_API_KEY",
        "INVOKE_BASE_URL",
    ]
    env = {key: os.environ[key] for key in env_keys if os.environ.get(key)}

    return DeployPlan(
        project_root=str(project_root),
        app_name=slug,
        entrypoint=entrypoint,
        modal_volume=f"{slug}-state",
        endpoint_name=modal_endpoint_label(slug),
        env=env,
        persistence={
            "kind": "modal_volume",
            "mount_path": "/state",
            "trace_path": "/state/traces.jsonl",
            "checkpoint_path": "/state/checkpoints.jsonl",
        },
        robustness={
            "schema_checks": True,
            "policy_checks": True,
            "state_reconciliation": True,
            "freeze_thaw_hitl": True,
            "structured_tracing": True,
            "onyx_supervisor": True,
        },
    )


def write_local_deploy_record(result: DeployResult) -> Path:
    root = Path(result.plan.project_root)
    state_dir = root / ".invoke"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "deployment.json"
    path.write_text(json.dumps(sanitized_result(result), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def copy_template(destination: str | Path, *, template: str = "claude-agent-sdk", force: bool = False) -> Path:
    """Scaffold an Invoke agent starter project."""

    destination = Path(destination).expanduser().resolve()
    template_root = Path(__file__).with_name("templates") / template
    if not template_root.exists():
        raise FileNotFoundError(f"Unknown Invoke template: {template}")
    if destination.exists() and any(destination.iterdir()) and not force:
        raise FileExistsError(f"{destination} is not empty. Pass force=True to overwrite template files.")
    destination.mkdir(parents=True, exist_ok=True)
    for source in template_root.rglob("*"):
        target = destination / source.relative_to(template_root)
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not force:
            continue
        shutil.copyfile(source, target)
    return destination


def deploy_claude_agent(
    project_path: str | Path,
    *,
    app_name: str | None = None,
    dry_run: bool = False,
    run_onyx: bool = True,
) -> DeployResult:
    """Build and optionally deploy a Claude Agent SDK project to Modal."""

    plan = build_deploy_plan(project_path, app_name=app_name)
    deployment_id = "dep_" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")

    trace_store = TraceStore(Path(plan.project_root) / ".invoke" / "traces.jsonl")
    boot_trace = build_boot_trace(plan.app_name, plan.entrypoint, plan.robustness)
    trace_store.append(boot_trace)

    suggestions = analyze_traces(trace_store.recent(limit=100)) if run_onyx else []

    from .sandbox import write_modal_source

    modal_source_path = write_modal_source(plan)
    dashboard_url = f"https://modal.com/apps/{plan.app_name}"

    if dry_run:
        result = DeployResult(
            success=True,
            plan=plan,
            deployment_id=deployment_id,
            status="planned",
            message="Deployment plan generated with Invoke tracing and Onyx supervisor enabled. Modal deploy was not executed.",
            dashboard_url=dashboard_url,
            trace_path=str(trace_store.path),
            modal_source_path=str(modal_source_path),
            onyx_suggestions=suggestions,
        )
        write_local_deploy_record(result)
        return result

    from .sandbox import deploy_modal_app

    modal_result = deploy_modal_app(plan)
    result = DeployResult(
        success=True,
        plan=plan,
        deployment_id=deployment_id,
        status="deployed",
        message="Agent deployed to Modal with Invoke tracing, persistence, and Onyx supervisor enabled.",
        modal_app_name=modal_result.get("app_name", plan.app_name),
        endpoint_url=modal_result.get("endpoint_url"),
        dashboard_url=modal_result.get("dashboard_url", dashboard_url),
        trace_path=str(trace_store.path),
        modal_source_path=str(modal_source_path),
        onyx_suggestions=suggestions,
    )
    write_local_deploy_record(result)
    return result


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Deploy a Claude Agent SDK project with Invoke.")
    parser.add_argument("path", help="Agent project path.")
    parser.add_argument("--app-name")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--template", action="store_true", help="Scaffold a starter project at path.")
    args = parser.parse_args()

    if args.template:
        path = copy_template(args.path)
        print(f"Created Claude Agent SDK template at {path}")
        return 0

    result = deploy_claude_agent(args.path, app_name=args.app_name, dry_run=args.dry_run)
    print(json.dumps(sanitized_result(result), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
