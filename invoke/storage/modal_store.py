"""Modal-volume trace storage helpers.

These functions are intentionally import-safe outside Modal. In a generated
Modal sandbox they operate against the mounted volume path, usually `/state`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .traces import AgentTrace


DEFAULT_STATE_DIR = Path("/state")


def _connect(state_dir: str | Path = DEFAULT_STATE_DIR) -> sqlite3.Connection:
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "traces.db")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS traces (
            trace_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            status TEXT NOT NULL,
            execution_id TEXT,
            action TEXT,
            stage TEXT,
            data TEXT NOT NULL
        )
        """
    )
    return conn


async def store_trace(trace: AgentTrace, *, state_dir: str | Path = DEFAULT_STATE_DIR) -> None:
    root = Path(state_dir)
    payload = trace.model_dump()
    conn = _connect(root)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO traces
            (trace_id, agent_id, agent_name, timestamp, status, execution_id, action, stage, data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace.trace_id,
                trace.agent_id,
                trace.agent_name,
                payload["timestamp"],
                trace.status,
                trace.execution_id,
                trace.action,
                trace.stage,
                json.dumps(payload, sort_keys=True),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    jsonl_path = root / "agent_traces.jsonl"
    with jsonl_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


async def get_recent_traces(agent_id: str, hours: int = 48, *, state_dir: str | Path = DEFAULT_STATE_DIR) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn = _connect(state_dir)
    try:
        cursor = conn.execute(
            "SELECT data FROM traces WHERE agent_id = ? AND timestamp > ? ORDER BY timestamp DESC LIMIT 200",
            (agent_id, cutoff),
        )
        return [json.loads(row[0]) for row in cursor.fetchall()]
    finally:
        conn.close()
