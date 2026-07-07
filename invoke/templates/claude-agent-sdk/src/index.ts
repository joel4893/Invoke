import { query } from "@anthropic-ai/claude-agent-sdk"

const prompt = process.argv.slice(2).join(" ") || "Summarize what this agent can do."

for await (const message of query({
  prompt,
  options: {
    allowedTools: [],
  },
})) {
  if ("result" in message) {
    console.log(message.result)
  }
}
