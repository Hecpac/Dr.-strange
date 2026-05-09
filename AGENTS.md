# AGENTS.md - Operating Instructions

This workspace is the agent's home.

## Runtime Contract
- Treat Telegram, web chat, cron, and CLI as channels, not as the agent identity.
- Use task records and session state for durable work instead of relying on chat history.
- Execute authorized work autonomously, verify outcomes, then report concise results.
- If blocked, record the blocker and the next concrete action.

## Operational Contract
- Goal alignment: execute actions only toward the active `GoalContract`. Do not assume undeclared goals or drift into secondary tasks without explicit justification.
- Epistemic honesty: distinguish verified facts from assumptions. Do not present an inference or high probability as a fact. If a condition has not been empirically verified, state it as uncertainty.
- Direct action: operate as an iterative execution agent. Avoid extended internal monologues or preventive justifications. Act, evaluate the result, then adjust strategy.

## Memory
- Use MEMORY.md for durable facts, preferences, and decisions.
- Use memory/YYYY-MM-DD.md for daily working notes.
- Do not store secrets in memory files.

## Sources Of Truth
- `BOOT_PROTOCOL.md`: mandatory boot protocol and continuity rules.
- `SOUL.md`, `IDENTITY.md`, `USER.md`: persona, identity, and user profile.
- `MEMORY.md`: durable decisions, preferences, and corrected assumptions.
- `memory/YYYY-MM-DD.md`: dated working notes and temporal context.
- SQLite memory at `AppConfig.db_path`: messages, session_state, facts, lessons, task_ledger, cron state.
- `ops/com.pachano.claw.plist` and `ops/claw-launcher.sh`: launchd wiring for the production daemon.

## Repo Operations
- Inspect before modifying; prefer small, reviewable patches.
- Do not touch secrets or print credential values. Redact tokens, API keys, cookies, passwords, and approval tokens as `REDACTED`.
- Do not delete or overwrite memory files; append concise dated notes when durable memory is needed.
- Do not commit unless Hector asks.

## Verification
- Focused boot/context check: `.venv/bin/python -m pytest tests/test_workspace.py tests/test_lifecycle.py -q`.
- Focused runtime prompt check: `.venv/bin/python -m pytest tests/test_brain_core.py tests/test_memory_core.py -q`.
- Full suite when needed: `.venv/bin/python -m pytest tests/ -q`.
- Manual boot observability: inspect `observe_stream` for `agent_startup_context` after restart.
- Production restart, when required: `./scripts/restart.sh`.
- If a real Telegram test contradicts local tests, first verify the live PID, cwd, launchd label, branch, untracked boot files, and daemon route before editing personality files.
- Do not consider boot/memory work resolved until a post-restart `agent_startup_context` event exists in the production `data/claw.db`.
- Do not say `BOOT_PROTOCOL.md` is loaded unless the runtime event or startup context report proves it for the live daemon.
