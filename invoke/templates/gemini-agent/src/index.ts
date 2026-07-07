const prompt = process.argv.slice(2).join(" ") || "Summarize what this agent can do."

const apiKey = process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY
const model = process.env.GEMINI_MODEL || "gemini-2.5-flash"

if (!apiKey) {
  console.error("Missing GEMINI_API_KEY or GOOGLE_API_KEY")
  process.exit(1)
}

const response = await fetch(
  `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${apiKey}`,
  {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      contents: [
        {
          role: "user",
          parts: [
            {
              text: prompt,
            },
          ],
        },
      ],
      generationConfig: {
        temperature: 0.2,
        maxOutputTokens: 800,
      },
    }),
  },
)

if (!response.ok) {
  console.error(`Gemini request failed ${response.status}: ${await response.text()}`)
  process.exit(1)
}

const payload = await response.json()
const text =
  payload?.candidates?.[0]?.content?.parts
    ?.map((part: { text?: string }) => part.text || "")
    .join("") || ""

console.log(text || JSON.stringify(payload))
