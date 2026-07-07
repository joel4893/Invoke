"""Structured tracing, policy checks, and freeze/thaw checkpoints."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@dataclass
class TraceEvent:
    step: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=utc_now)


@dataclass
class ExecutionTrace:
    execution_id: str
    agent_id: str
    action: str
    status: str
    risk: str
    events: list[TraceEvent] = field(default_factory=list)
    final_outcome: str | None = None
    created_at: str = field(default_factory=utc_now)


@dataclass
class PolicyDecision:
    effect: str
    risk: str
    reason: str
    requires_approval: bool = False
    guardrails: list[str] = field(default_factory=list)


@dataclass
class Checkpoint:
    checkpoint_id: str
    execution_id: str
    action: str
    params: dict[str, Any]
    context_snapshot: dict[str, Any]
    created_at: str = field(default_factory=utc_now)


class TraceStore:
    """Append-only JSONL trace store."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, trace: ExecutionTrace | dict[str, Any]) -> None:
        payload = asdict(trace) if hasattr(trace, "__dataclass_fields__") else trace
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True) + "\n")

    def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        traces: list[dict[str, Any]] = []
        for line in lines:
            try:
                traces.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return traces


def evaluate_policy(action: str, params: dict[str, Any]) -> PolicyDecision:
    lowered = action.lower()
    sql = str(params.get("sql", "")).lower()
    if action == "database.execute" or "disable row level security" in sql or "drop table" in sql:
        return PolicyDecision(
            effect="block",
            risk="high",
            reason="Direct database execution can bypass application policy or row-level security.",
            guardrails=["policy_block", "sandbox_required"],
        )
    if any(word in lowered for word in ("delete", "refund", "charge", "transfer")):
        return PolicyDecision(
            effect="require_approval",
            risk="high",
            reason="Financial or destructive action requires approval and reconciliation.",
            requires_approval=True,
            guardrails=["approval", "idempotency_key", "state_reconciliation"],
        )
    return PolicyDecision(effect="allow", risk="low", reason="No blocking policy matched.", guardrails=["trace"])


def reconcile_state(expected: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    drift = []
    for key, expected_value in expected.items():
        if live.get(key) != expected_value:
            drift.append({"key": key, "expected": expected_value, "live": live.get(key)})
    return {
        "status": "changed" if drift else "valid",
        "drift": drift,
        "checked_at": utc_now(),
    }


def freeze_execution(execution_id: str, action: str, params: dict[str, Any], context: dict[str, Any]) -> Checkpoint:
    return Checkpoint(
        checkpoint_id="freeze_" + uuid.uuid4().hex[:12],
        execution_id=execution_id,
        action=action,
        params=params,
        context_snapshot=context,
    )


def thaw_checkpoint(checkpoint: Checkpoint, live_context: dict[str, Any]) -> dict[str, Any]:
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "execution_id": checkpoint.execution_id,
        "revalidation": reconcile_state(checkpoint.context_snapshot, live_context),
        "thawed_at": utc_now(),
    }


def build_boot_trace(app_name: str, entrypoint: str, robustness: dict[str, bool]) -> ExecutionTrace:
    trace = ExecutionTrace(
        execution_id="exec_" + uuid.uuid4().hex[:12],
        agent_id=app_name,
        action="invoke.deploy",
        status="planned",
        risk="low",
        final_outcome="deployment_plan_recorded",
    )
    trace.events.extend(
        [
            TraceEvent("project_loaded", "ok", {"entrypoint": entrypoint}),
            TraceEvent("persistence_selected", "ok", {"kind": "modal_volume"}),
            TraceEvent("robustness_layer_enabled", "ok", robustness),
        ]
    )
    return trace
