"""Example Invoke-instrumented Python agent entrypoint."""

from anthropic import AsyncAnthropic

from invoke.sdk import InvokeTracer, set_tracer, with_invoke_tracing


set_tracer(InvokeTracer(agent_id="my-agent", agent_name="my-agent"))


@with_invoke_tracing
async def run_agent(input_data: dict):
    client = AsyncAnthropic()
    prompt = input_data.get("prompt", "Summarize what this agent can do.")
    message = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return message
