"""Structured trace record used by Invoke and Onyx."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TraceError:
    type: str | None = None
    message: str | None = None

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentTrace:
    trace_id: str
    agent_id: str
    agent_name: str
    timestamp: datetime = field(default_factory=utc_now)
    status: str = "event"
    duration_ms: int | None = None
    input: dict[str, Any] | None = None
    output: Any = None
    error: TraceError | dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    reasoning_steps: list[str] = field(default_factory=list)
    token_usage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    execution_id: str | None = None
    action: str | None = None
    stage: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["timestamp"] = self.timestamp.isoformat()
        if isinstance(self.error, TraceError):
            payload["error"] = self.error.model_dump()
        return payload

    def model_dump_json(self) -> str:
        return json.dumps(self.model_dump(), sort_keys=True)

    @classmethod
    def from_event(
        cls,
        *,
        trace_id: str,
        agent_id: str,
        agent_name: str,
        status: str,
        data: dict[str, Any],
        execution_id: str | None = None,
        action: str | None = None,
        stage: str | None = None,
    ) -> "AgentTrace":
        return cls(
            trace_id=trace_id,
            agent_id=agent_id,
            agent_name=agent_name,
            status=status,
            execution_id=execution_id,
            action=action,
            stage=stage,
            data=data,
        )
