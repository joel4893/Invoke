"""Onyx worker loop for watching agent traces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from invoke.onyx.analyzer import OnyxMode, onyx_analyze_failures
from invoke.storage.modal_store import get_recent_traces


@dataclass
class OnyxWorker:
    agent_id: str
    mode: OnyxMode = "auto"
    hours: int = 48

    async def analyze(self, llm_client: Any | None = None) -> dict[str, Any]:
        traces = await get_recent_traces(self.agent_id, hours=self.hours)
        result = await onyx_analyze_failures(traces, llm_client=llm_client, mode=self.mode)
        result["agent_id"] = self.agent_id
        result["trace_count"] = len(traces)
        result["status"] = "watching"
        return result
