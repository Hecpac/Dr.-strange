# AGENTS.md - Operating Instructions

This workspace is the agent's home.

## Runtime Contract
- Treat Telegram, web chat, cron, and CLI as channels, not as the agent identity.
- Use task records and session state for durable work instead of relying on chat history.
- Execute authorized work autonomously, verify outcomes, then report concise results.
- If blocked, record the blocker and the next concrete action.

## Memory
- Use MEMORY.md for durable facts, preferences, and decisions.
- Use memory/YYYY-MM-DD.md for daily working notes.
- Do not store secrets in memory files.
