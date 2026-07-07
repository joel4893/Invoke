"""Automatic tracing wrapper for deployed agent runtimes."""

from __future__ import annotations

import functools
import inspect
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from invoke.storage.modal_store import store_trace
from invoke.storage.traces import AgentTrace, TraceError


class InvokeTracer:
    """Automatic tracing wrapper for Claude Agent SDK-style agents."""

    def __init__(self, agent_id: str, agent_name: str, *, state_dir: str | None = None):
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.state_dir = state_dir
        self._start = time.time()

    async def record(
        self,
        input_data: dict[str, Any],
        output: Any = None,
        error: Exception | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_steps: list[str] | None = None,
        token_usage: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        trace = AgentTrace(
            trace_id=str(uuid.uuid4()),
            agent_id=self.agent_id,
            agent_name=self.agent_name,
            timestamp=datetime.now(timezone.utc),
            status="failed" if error else "success",
            duration_ms=int((time.time() - self._start) * 1000),
            input=input_data,
            output=safe_json(output) if not error else None,
            error=TraceError(type=type(error).__name__, message=str(error)) if error else None,
            tool_calls=tool_calls or [],
            reasoning_steps=reasoning_steps or [],
            token_usage=token_usage or {},
            metadata=metadata or {},
            action=str(input_data.get("action") or input_data.get("prompt") or "agent.run")[:200],
            stage="agent_runtime",
            data={
                "input": input_data,
                "output": safe_json(output) if not error else None,
                "error": {"type": type(error).__name__, "message": str(error)} if error else None,
                "tool_calls": tool_calls or [],
                "reasoning_steps": reasoning_steps or [],
                "token_usage": token_usage or {},
                "metadata": metadata or {},
            },
        )
        if self.state_dir:
            await store_trace(trace, state_dir=self.state_dir)
        else:
            await store_trace(trace)


tracer: InvokeTracer | None = None


def set_tracer(next_tracer: InvokeTracer | None) -> None:
    global tracer
    tracer = next_tracer


def with_invoke_tracing(func: Callable[..., Any]):
    """Decorator to wrap a main agent run function."""

    @functools.wraps(func)
    async def async_wrapper(*args: Any, **kwargs: Any):
        if not tracer:
            result = func(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result

        tracer._start = time.time()
        input_data = normalize_input(args, kwargs)
        try:
            result = func(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            await tracer.record(
                input_data=input_data,
                output=result,
                tool_calls=extract_tool_calls(result),
                reasoning_steps=extract_reasoning(result),
                token_usage=extract_token_usage(result),
            )
            return result
        except Exception as exc:
            await tracer.record(input_data=input_data, error=exc)
            raise

    return async_wrapper


def normalize_input(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    if kwargs:
        return dict(kwargs)
    if args and isinstance(args[0], dict):
        return args[0]
    if args:
        return {"args": [safe_json(arg) for arg in args]}
    return {}


def safe_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): safe_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_json(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return safe_json(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        try:
            return safe_json(vars(value))
        except Exception:
            pass
    return str(value)


def extract_tool_calls(result: Any) -> list[dict[str, Any]]:
    try:
        value = getattr(result, "tool_calls", None)
        if isinstance(value, list):
            calls = []
            for item in value:
                normalized = safe_json(item)
                if isinstance(normalized, dict):
                    calls.append(normalized)
            return calls
    except Exception:
        return []
    return []


def extract_reasoning(result: Any) -> list[str]:
    try:
        value = getattr(result, "reasoning", None)
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str):
            return [value]
    except Exception:
        return []
    return []


def extract_token_usage(result: Any) -> dict[str, Any]:
    try:
        usage = getattr(result, "usage", None) or getattr(result, "token_usage", None)
        if isinstance(usage, dict):
            return usage
        if usage is not None and hasattr(usage, "__dict__"):
            return safe_json(vars(usage))
    except Exception:
        return {}
    return {}
