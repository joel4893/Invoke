"""Onyx Lite: production trace analysis and concrete repair suggestions."""

from __future__ import annotations

import json
import os
import re
import inspect
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal


OnyxMode = Literal["deterministic", "intelligence", "auto"]
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


ONYX_FAILURE_ANALYSIS_PROMPT = """
You are Onyx, a senior AI systems engineer and agent supervisor.
You are extremely good at debugging and improving Claude Agent SDK agents running in production.

You have access to the last {num_runs} runs of the agent, including:
- Full traces with reasoning steps, tool calls, inputs, outputs, errors
- Timestamps, duration, token usage
- Environment context (tools available, policies, schemas)

Recent failures detected:
{failure_summary}

=== TASK ===
Analyze these failures and provide the single most valuable improvement the agent can make right now.

Focus especially on:
- Repeated patterns, for example the same error 16 times
- Prompt issues, missing instructions, bad formatting
- Tool usage problems, wrong parameters, missing validation
- State management or context loss
- Schema violations
- Hallucinations or bad assumptions

=== RESPONSE FORMAT strict JSON ===
{{
  "observation": "Short, clear summary of the root cause. Mention how many times it happened.",
  "suggested_fix": "Very specific, actionable change. Include code or prompt snippet if possible.",
  "fix_type": "prompt_improvement | tool_wrapper | retry_strategy | schema_validation | state_handling | other",
  "expected_impact": "high/medium/low on failure rate plus brief reason",
  "confidence": 85,
  "one_click_patch": "Optional: exact string to patch in the system prompt or tool definition"
}}

Be concise, professional, and ruthless about impact. Only suggest changes that will clearly reduce failures.
""".strip()


@dataclass
class OnyxSuggestion:
    title: str
    severity: str
    reason: str
    apply: dict[str, Any] = field(default_factory=dict)
    observation: str = ""
    suggested_fix: str = ""
    fix_type: str = "other"
    expected_impact: str = "low"
    confidence: int = 60
    one_click_patch: str | None = None
    evidence_count: int = 0
    source: str = "deterministic_trace_analysis"


@dataclass
class OnyxRoute:
    mode: OnyxMode
    reason: str
    requires_llm: bool = False


@dataclass
class FailureGroup:
    key: str
    title: str
    fix_type: str
    severity: str
    traces: list[dict[str, Any]] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.traces)


META_TRACE_ACTIONS = {"invoke.deploy"}
META_TRACE_OUTCOMES = {"deployment_plan_recorded"}


def _is_runtime_trace(trace: dict[str, Any]) -> bool:
    """Return true when a trace represents agent execution, not Invoke setup."""

    action = str(trace.get("action") or trace.get("tool") or "").lower()
    outcome = str(trace.get("final_outcome") or trace.get("status") or "").lower()
    if action in META_TRACE_ACTIONS:
        return False
    if outcome in META_TRACE_OUTCOMES:
        return False
    return True


def _runtime_traces(traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [trace for trace in traces if _is_runtime_trace(trace)]


def _flatten_events(trace: dict[str, Any]) -> list[dict[str, Any]]:
    events = trace.get("events") or trace.get("trace") or []
    if isinstance(events, list):
        return [event for event in events if isinstance(event, dict)]
    return []


def _error_message(trace: dict[str, Any]) -> str:
    error = trace.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type") or "")[:240]
    if error:
        return str(error)[:240]
    for event in _flatten_events(trace):
        if event.get("status") in {"error", "failed", "timeout"}:
            detail = event.get("detail")
            if isinstance(detail, dict):
                return str(detail.get("message") or detail.get("error") or event.get("step") or "")[:240]
            return str(event.get("step") or event.get("status") or "")[:240]
    return ""


def _classify_failure(trace: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """Return key/title/fix_type/severity for one failed trace."""

    outcome = str(trace.get("final_outcome") or trace.get("status") or "").lower()
    action = str(trace.get("action") or trace.get("tool") or "").lower()
    error = trace.get("error") if isinstance(trace.get("error"), dict) else {}
    error_type = str(error.get("type") or error.get("code") or "").lower()
    message = (_error_message(trace) or "").lower()
    steps = Counter(str(event.get("step") or "").lower() for event in _flatten_events(trace))
    statuses = Counter(str(event.get("status") or "").lower() for event in _flatten_events(trace))

    if "timeout" in outcome or "timeout" in error_type or "timeout" in message or steps.get("tool_timeout"):
        return ("timeout_unknown_effect", "Unknown outcome after timeout", "retry_strategy", "high")

    if (
        "idempotent_replay" in outcome
        or "duplicate" in outcome
        or steps.get("duplicate_retry_detected")
        or "duplicate" in message
    ):
        return ("duplicate_retry", "Duplicate retry pattern", "tool_wrapper", "high")

    if "blocked_by_policy" in outcome or "policy" in outcome or action == "database.execute":
        return ("policy_block", "Policy-blocked unsafe action", "tool_wrapper", "high")

    if "schema" in outcome or "schema" in error_type or "validation" in message or "bad request" in message:
        return ("schema_validation", "Schema validation failure", "schema_validation", "medium")

    if "drift" in outcome or steps.get("state_revalidation") or steps.get("state_reconciled"):
        return ("state_drift", "State changed before resume", "state_handling", "high")

    if "auth" in error_type or "401" in message or "unauthorized" in message or "forbidden" in message:
        return ("auth_expired", "Tool authentication failure", "tool_wrapper", "medium")

    if statuses.get("error") or statuses.get("failed") or error or outcome in {"failed", "error"}:
        return ("tool_error", "Repeated tool error", "other", "medium")

    return None


def _group_failures(traces: list[dict[str, Any]]) -> dict[str, FailureGroup]:
    groups: dict[str, FailureGroup] = {}
    for trace in traces[-50:]:
        classified = _classify_failure(trace)
        if not classified:
            continue
        key, title, fix_type, severity = classified
        group = groups.setdefault(key, FailureGroup(key, title, fix_type, severity))
        group.traces.append(trace)
        example = _error_message(trace)
        if example and example not in group.examples:
            group.examples.append(example)
    return groups


def summarize_failures(traces: list[dict[str, Any]], *, min_count: int = 3) -> str:
    """Summarize repeated failures for Onyx prompts and dashboards."""

    traces = _runtime_traces(traces)
    if not traces:
        return "No agent runtime traces yet. Deployment/setup traces are ignored."

    groups = _group_failures(traces)
    lines: list[str] = []
    for group in sorted(groups.values(), key=lambda item: item.count, reverse=True):
        if group.count < min_count:
            continue
        example = f" Example: {group.examples[0]}" if group.examples else ""
        lines.append(f"- {group.title} happened {group.count} times.{example}")
    return "\n".join(lines) if lines else "No significant repeated failures."


def should_use_intelligence(traces: list[dict[str, Any]]) -> OnyxRoute:
    """Choose whether Onyx needs Claude-level context reasoning.

    Deterministic Onyx is best for known production failure shapes: timeouts,
    duplicate retries, schema errors, auth drift, policy blocks, and stale state.
    Intelligence Onyx is for ambiguous trace clusters where prompts, planning,
    tool sequencing, or agent assumptions may be the root cause.
    """

    if not traces:
        return OnyxRoute("deterministic", "No traces available yet.")

    traces = _runtime_traces(traces)
    if not traces:
        return OnyxRoute("deterministic", "Only deployment/setup traces are available; waiting for agent runtime traces.")

    groups = _group_failures(traces)
    repeated_groups = [group for group in groups.values() if group.count >= 3]
    known_keys = {
        "timeout_unknown_effect",
        "duplicate_retry",
        "policy_block",
        "schema_validation",
        "state_drift",
        "auth_expired",
    }
    if repeated_groups and all(group.key in known_keys for group in repeated_groups):
        names = ", ".join(group.key for group in repeated_groups[:3])
        return OnyxRoute("deterministic", f"Known repeated failure pattern detected: {names}.")

    promptish_markers = ("hallucinat", "wrong tool", "bad assumption", "missing context", "plan", "reasoning")
    trace_text: list[str] = []
    for trace in traces[-20:]:
        trace_text.extend(
            [
                str(trace.get("action") or trace.get("tool") or ""),
                _error_message(trace),
                " ".join(str(event.get("step") or "") for event in _flatten_events(trace)),
            ]
        )
    all_text = " ".join(trace_text).lower()
    if any(marker in all_text for marker in promptish_markers):
        return OnyxRoute(
            "intelligence",
            "Trace text points to planning, prompt, tool-selection, or context quality issues.",
            requires_llm=True,
        )

    if repeated_groups:
        names = ", ".join(group.key for group in repeated_groups[:3])
        return OnyxRoute(
            "intelligence",
            f"Repeated but ambiguous failure cluster detected: {names}.",
            requires_llm=True,
        )

    unknown_failures = [
        trace
        for trace in traces[-20:]
        if (trace.get("error") or str(trace.get("final_outcome") or trace.get("status") or "").lower() in {"failed", "error"})
        and _classify_failure(trace) is None
    ]
    if len(unknown_failures) >= 2:
        return OnyxRoute("intelligence", "Multiple unclassified failures need context-aware analysis.", requires_llm=True)

    return OnyxRoute("deterministic", "No ambiguous repeated failure pattern detected.")


def _build_suggestion(group: FailureGroup) -> OnyxSuggestion:
    if group.key == "timeout_unknown_effect":
        return OnyxSuggestion(
            title="Require reconciliation before retry",
            severity="high",
            reason=f"Seen this unknown-effect timeout pattern {group.count} times. A blind retry can duplicate a side effect.",
            observation=f"Timeouts left the tool outcome unknown {group.count} times, so the agent could retry after the action already succeeded.",
            suggested_fix=(
                "Patch the tool wrapper so every retry first calls a reconcile function and returns already_succeeded "
                "when live state confirms the side effect happened."
            ),
            fix_type="retry_strategy",
            expected_impact="high on duplicate side effects because retries become state-aware",
            confidence=90,
            one_click_patch='{"retry_strategy":"reconcile_before_retry","unknown_effect":true}',
            evidence_count=group.count,
            apply={"retry_strategy": "reconcile_before_retry", "unknown_effect": True},
        )

    if group.key == "duplicate_retry":
        return OnyxSuggestion(
            title="Require idempotency keys for this action",
            severity="high",
            reason=f"Seen this duplicate-action pattern {group.count} times. The same request can create multiple tickets, charges, or messages.",
            observation=f"The agent repeated an action {group.count} times after losing or mistrusting the first response.",
            suggested_fix="Require an idempotency key derived from stable business fields before executing this tool.",
            fix_type="tool_wrapper",
            expected_impact="high on duplicate work because replay becomes deterministic",
            confidence=88,
            one_click_patch='{"idempotency":{"mode":"required","key_fields":["customer_id","amount","action"]}}',
            evidence_count=group.count,
            apply={"idempotency": {"mode": "required", "key_fields": ["customer_id", "amount", "action"]}},
        )

    if group.key == "policy_block":
        return OnyxSuggestion(
            title="Move this policy block into preflight",
            severity="high",
            reason=f"Seen unsafe actions blocked {group.count} times. The agent should learn before execution, not after planning.",
            observation=f"Onyx saw {group.count} attempts to execute actions that policy would never allow.",
            suggested_fix="Add a preflight policy preview that blocks direct database execution and suggests a scoped API/tool instead.",
            fix_type="tool_wrapper",
            expected_impact="medium-high on operator time because impossible actions stop before tool execution",
            confidence=86,
            one_click_patch='{"preflight":{"policy_preview":true,"block":["database.execute","*.delete"]}}',
            evidence_count=group.count,
            apply={"preflight": {"policy_preview": True, "block": ["database.execute", "*.delete"]}},
        )

    if group.key == "schema_validation":
        return OnyxSuggestion(
            title="Add strict schema validation before tool calls",
            severity="medium",
            reason=f"Seen malformed parameters {group.count} times. The agent is guessing tool input shape.",
            observation=f"Schema or validation failures occurred {group.count} times, usually before the tool could do useful work.",
            suggested_fix="Validate params locally and return a repairable error showing required fields and allowed enum values.",
            fix_type="schema_validation",
            expected_impact="medium on failure rate because bad calls become self-correcting",
            confidence=82,
            one_click_patch='{"schema_validation":{"mode":"strict","repair_hints":true}}',
            evidence_count=group.count,
            apply={"schema_validation": {"mode": "strict", "repair_hints": True}},
        )

    if group.key == "state_drift":
        return OnyxSuggestion(
            title="Revalidate state before thawing approvals",
            severity="high",
            reason=f"Seen state drift {group.count} times. Human approval can become stale while the world changes.",
            observation=f"Pending work resumed against changed state {group.count} times.",
            suggested_fix="Attach a state snapshot to each checkpoint and require revalidation before approval resumes execution.",
            fix_type="state_handling",
            expected_impact="high on unsafe resumes because stale approvals are requeued",
            confidence=87,
            one_click_patch='{"freeze_thaw":{"revalidate_before_resume":true,"drift_action":"requeue"}}',
            evidence_count=group.count,
            apply={"freeze_thaw": {"revalidate_before_resume": True, "drift_action": "requeue"}},
        )

    if group.key == "auth_expired":
        return OnyxSuggestion(
            title="Classify auth failures and refresh credentials",
            severity="medium",
            reason=f"Seen auth failures {group.count} times. The agent is treating credential drift like a normal tool error.",
            observation=f"Credential or authorization failures happened {group.count} times.",
            suggested_fix="Classify 401/403 separately, refresh credentials once, then escalate with the provider and scope.",
            fix_type="tool_wrapper",
            expected_impact="medium on recovery rate because expired credentials stop causing noisy retries",
            confidence=80,
            one_click_patch='{"auth":{"classify":["401","403"],"refresh_once":true,"escalate_on_failure":true}}',
            evidence_count=group.count,
            apply={"auth": {"classify": ["401", "403"], "refresh_once": True, "escalate_on_failure": True}},
        )

    return OnyxSuggestion(
        title="Add targeted handling for repeated tool errors",
        severity=group.severity,
        reason=f"Seen this tool error {group.count} times. It is now a pattern, not noise.",
        observation=f"A repeated tool failure happened {group.count} times.",
        suggested_fix="Add a wrapper-specific classifier so the agent gets a repairable error instead of retrying blindly.",
        fix_type=group.fix_type,
        expected_impact="medium on failure rate because repeated errors become classified",
        confidence=72,
        one_click_patch='{"tool_errors":{"classify_repeated":true,"repair_hints":true}}',
        evidence_count=group.count,
        apply={"tool_errors": {"classify_repeated": True, "repair_hints": True}},
    )


def deterministic_analyze_traces(traces: list[dict[str, Any]]) -> list[OnyxSuggestion]:
    """Detect repeated failures and emit concrete, one-click fixes."""

    if not traces:
        return []

    runtime = _runtime_traces(traces)
    if not runtime:
        return [
            OnyxSuggestion(
                title="No agent runtime traces yet",
                severity="info",
                reason="Only deployment and setup traces exist. Onyx ignores those because they do not describe agent behavior.",
                observation=f"Ignored {len(traces)} deploy/setup traces. No executed agent runs have been recorded yet.",
                suggested_fix="Send one request to the deployed Modal endpoint, then rerun Onyx after the endpoint writes runtime traces.",
                fix_type="other",
                expected_impact="low until a real agent run exists",
                confidence=75,
                evidence_count=0,
                apply={},
            )
        ]

    groups = _group_failures(runtime)
    suggestions = [_build_suggestion(group) for group in groups.values() if group.count >= 3]
    suggestions.sort(key=lambda item: (item.severity != "high", -item.evidence_count, -item.confidence))

    if suggestions:
        return suggestions

    outcomes = Counter(str(trace.get("final_outcome") or trace.get("status") or "unknown") for trace in runtime)
    return [
        OnyxSuggestion(
            title="No repeated failure pattern detected",
            severity="info",
            reason="Recent traces do not show recurring timeout, duplicate retry, or policy-block patterns.",
            observation=f"Reviewed {len(runtime)} recent agent runs. Most common outcome: {outcomes.most_common(1)[0][0]}.",
            suggested_fix="Keep collecting structured traces. Onyx will suggest a patch once the same failure appears at least 3 times.",
            fix_type="other",
            expected_impact="low until repeated failures appear",
            confidence=70,
            evidence_count=0,
            apply={},
        )
    ]


def analyze_traces(traces: list[dict[str, Any]]) -> list[OnyxSuggestion]:
    """Backward-compatible deterministic Onyx analysis."""

    return deterministic_analyze_traces(traces)


def best_improvement(traces: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the single most valuable Onyx improvement as strict JSON data."""

    suggestions = deterministic_analyze_traces(traces)
    if not suggestions:
        return {
            "observation": "No traces available yet.",
            "suggested_fix": "Run the agent through Invoke so Onyx can inspect structured executions.",
            "fix_type": "other",
            "expected_impact": "low until traces exist",
            "confidence": 60,
            "one_click_patch": "",
        }
    top = suggestions[0]
    return {
        "observation": top.observation or top.reason,
        "suggested_fix": top.suggested_fix or top.reason,
        "fix_type": top.fix_type,
        "expected_impact": top.expected_impact,
        "confidence": top.confidence,
        "one_click_patch": top.one_click_patch or json.dumps(top.apply, sort_keys=True),
    }


def _gemini_api_key_from_env() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def _extract_llm_text(response: Any) -> str:
    if isinstance(response, dict):
        candidates = response.get("candidates")
        if isinstance(candidates, list) and candidates:
            content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
            parts = content.get("parts") if isinstance(content, dict) else None
            if isinstance(parts, list):
                return "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))
        return str(response.get("content") or response.get("text") or "")

    content = getattr(response, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            text = getattr(part, "text", None)
            if isinstance(text, str):
                parts.append(text)
            elif isinstance(part, dict):
                parts.append(str(part.get("text", "")))
        if parts:
            return "".join(parts)

    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
    return str(response)


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


async def _call_gemini(prompt: str, *, model: str) -> dict[str, Any]:
    try:
        import httpx
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Gemini intelligence analysis requires httpx. Install with `pip install httpx` "
            "or run deterministic Onyx without GEMINI_API_KEY."
        ) from exc

    api_key = _gemini_api_key_from_env()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "Return only valid JSON. Do not wrap it in markdown. "
                            "Do not include commentary outside the JSON object.\n\n"
                            + prompt
                        )
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 900,
            "responseMimeType": "application/json",
        },
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, params={"key": api_key}, json=body)
        response.raise_for_status()
        return response.json()


async def intelligence_analyze_failures(
    traces: list[dict[str, Any]],
    llm_client: Any | None = None,
    *,
    model: str = DEFAULT_GEMINI_MODEL,
) -> dict[str, Any]:
    """Gemini-backed Onyx for context-heavy failures."""

    if llm_client is None and not _gemini_api_key_from_env():
        fallback = best_improvement(traces)
        fallback["mode"] = "deterministic"
        fallback["source"] = "deterministic_trace_analysis"
        fallback["error"] = "Intelligence Onyx requires GEMINI_API_KEY or GOOGLE_API_KEY."
        return fallback

    failure_summary = summarize_failures(traces)
    prompt = ONYX_FAILURE_ANALYSIS_PROMPT.format(num_runs=len(traces), failure_summary=failure_summary)

    try:
        if llm_client is not None:
            response = llm_client(prompt)
            if inspect.isawaitable(response):
                response = await response
        else:
            response = await _call_gemini(prompt, model=os.environ.get("GEMINI_MODEL", model))
        analysis = _parse_json_object(_extract_llm_text(response))
    except Exception as exc:  # noqa: BLE001 - this boundary must never break deploy.
        fallback = best_improvement(traces)
        fallback["mode"] = "deterministic"
        fallback["source"] = "deterministic_trace_analysis"
        fallback["error"] = f"Gemini intelligence analysis failed: {exc}"
        return fallback

    required = {"observation", "suggested_fix", "fix_type", "expected_impact", "confidence"}
    if not required.issubset(analysis):
        fallback = best_improvement(traces)
        fallback["mode"] = "deterministic"
        fallback["source"] = "deterministic_trace_analysis"
        fallback["error"] = "Onyx analysis was missing required fields."
        return fallback
    analysis.setdefault("mode", "intelligence")
    analysis.setdefault("source", "gemini_context_analysis")
    analysis.setdefault("provider", "gemini")
    analysis.setdefault("model", os.environ.get("GEMINI_MODEL", model))
    return analysis


async def onyx_analyze_failures(
    traces: list[dict[str, Any]],
    llm_client: Any | None = None,
    *,
    mode: OnyxMode = "auto",
    model: str = DEFAULT_GEMINI_MODEL,
) -> dict[str, Any]:
    """Main entry point for Onyx analysis.

    Modes:
    - deterministic: fast local analysis for known failure classes.
    - intelligence: Gemini-backed analysis for ambiguous context-heavy failures.
    - auto: deterministic by default, Gemini only when context is required.
    """

    if mode == "deterministic":
        result = best_improvement(traces)
        result["mode"] = "deterministic"
        result["source"] = "deterministic_trace_analysis"
        return result

    if mode == "intelligence":
        return await intelligence_analyze_failures(traces, llm_client, model=model)

    route = should_use_intelligence(traces)
    if route.requires_llm:
        result = await intelligence_analyze_failures(traces, llm_client, model=model)
        result["route_reason"] = route.reason
        if result.get("error"):
            result["intelligence_available"] = False
            result["intelligence_reason"] = result["error"]
        return result

    result = best_improvement(traces)
    result["mode"] = "deterministic"
    result["source"] = "deterministic_trace_analysis"
    result["route_reason"] = route.reason
    return result
