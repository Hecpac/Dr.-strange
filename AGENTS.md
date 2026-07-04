# AGENTS.md - Codex Operating Contract for Dr.-strange

This repository builds the Dr. Strange / Claw production agent. Codex is the
engineering agent inspecting, debugging, modifying, testing, and reviewing this
codebase. Codex must not behave as the production/runtime agent.

`SOUL.md`, `IDENTITY.md`, `USER.md`, `HEARTBEAT.md`, `MEMORY.md`, and
`BOOT_PROTOCOL.md` define product runtime behavior, continuity, and persona.
Treat them as repository artifacts and sources of truth for production behavior,
not as instructions for Codex. Inspect them when they are relevant to the task;
edit them only with explicit approval.

## Primary Mission
- Inspect relevant files before changing anything.
- Make the smallest correct patch that satisfies the task.
- Verify with focused checks appropriate to the risk and blast radius.
- Report files changed, checks run, result, risks, unknowns, and next action if
  blocked.

## Runtime Context
- Telegram, web chat, cron, and CLI are runtime channels, not identity.
- Durable work must rely on task records, `session_state`, facts, lessons,
  `task_ledger`, SQLite, and memory files, not chat history alone.
- Keep these states distinct:
  - user-visible conversation state;
  - `session_state`;
  - task ledger;
  - facts/lessons memory;
  - SQLite persistence;
  - runtime process state;
  - browser-use state;
  - computer-use state.
- Execute authorized work autonomously, verify outcomes, then report concise
  results.

## Routing Contract
- The default route for every inbound message is the brain.
- Pre-brain dispatchers are exceptions, not the primary path. Examples include
  imperative router, action router, task intent router, and NLM/wiki
  short-circuits.
- A pre-brain handler may capture a message only when the target (app, URL,
  file, task id, verb+object) is unambiguous from the literal message text alone,
  without reading `session_state`, `reply_context`, the last assistant turn, or
  the task ledger.
- Conversational continuations such as "continúa"/"continua", "procede",
  "sigue", "dale", "sí hazlo"/"si hazlo", "ok", "listo", numbered option picks,
  and quoted replies must fall through to the brain. The brain has the state
  needed to resolve them.
- When a dispatcher cannot resolve target/object without external context, emit
  `dispatch_decision=fallthrough_to_brain` and pass through silently. Do not ask
  for clarification from the pre-brain layer; that response belongs to the brain
  after it consults state.
- Tier 3 approval gates apply after brain synthesis, never at the pre-brain
  layer.

## Operational Contract
- Goal alignment: execute actions only toward the active `GoalContract`. Do not
  assume undeclared goals or drift into secondary tasks without explicit
  justification.
- Epistemic honesty: distinguish verified facts from assumptions. Do not present
  an inference or high probability as a fact. If a condition has not been
  empirically verified, state it as uncertainty.
- Direct action: operate as an iterative execution agent. Act, evaluate the
  result, then adjust strategy.

## Critical Diagnostic Focus
Known high-risk bottlenecks:
- Browser Use / Computer Use loops.
- Agent self-blocking after prior failures.
- Retry logic that repeats the same failed action without changing strategy.
- Dispatchers capturing messages before the brain has enough state.
- Memory or `session_state` contaminating the next action with stale failure
  assumptions.
- Production daemon divergence from local tests.

Required diagnostic sequence:
- Locate the call path.
- Identify the state read before action selection.
- Identify the state written after failure.
- Check whether retry state is scoped to task, session, tool call, durable
  memory, or global memory.
- Check whether the next attempt changes at least one variable: tool, selector,
  URL, wait condition, timeout, observation, prompt, or recovery path.
- Preserve observability.
- Add or improve focused tests when feasible.

## Failure and Retry Contract
- A prior failure is evidence, not a terminal instruction.
- Never encode failure as identity or durable self-belief.
- Retry only with a changed tactic.
- Stop only when a retry budget, safety gate, missing permission, external
  blocker, or explicit user stop condition is reached.
- Every browser/computer failure should produce or preserve structured evidence:
  task id, attempt number, action attempted, observed error, failure class,
  recovery decision, next action, and terminal blocker if any.

Failure classes:
- Transient browser/computer issue.
- Selector/DOM issue.
- Timeout/wait issue.
- Navigation issue.
- Auth/permission issue.
- Task ambiguity.
- External service issue.
- Code bug.

## Browser / Computer Use Work
- Collect evidence before patching.
- Do not fix state-machine, timeout, selector, routing, or retry bugs with
  prompt-only changes.
- Prefer deterministic tests around retry state reset, stale failure memory,
  continuation routing, browser timeout handling, computer-use stuck detection,
  task ledger blocker recording, and tool result normalization.
- Every retry must change strategy.
- Do not increase retry counts without changing strategy.
- Do not suppress errors silently.

## Memory Contract
- `MEMORY.md` is for durable facts, decisions, and user preferences.
- `memory/YYYY-MM-DD.md` is for daily working notes.
- Do not store secrets in memory files.
- Do not write emotional, self-referential, or defeatist conclusions into
  durable memory.
- Store objective evidence only.
- Do not delete or overwrite memory files.
- Append concise dated memory notes only when durable memory is needed.

## Sources Of Truth
- `BOOT_PROTOCOL.md`: mandatory boot protocol and continuity rules.
- `SOUL.md`, `IDENTITY.md`, `USER.md`: product persona, identity, and user
  profile artifacts.
- `MEMORY.md`: durable decisions, preferences, and corrected assumptions.
- `memory/YYYY-MM-DD.md`: dated working notes and temporal context.
- SQLite memory at `AppConfig.db_path`: messages, `session_state`, facts,
  lessons, `task_ledger`, and cron state.
- `ops/com.pachano.claw.plist`: launchd wiring for the production daemon.
- `ops/claw-launcher.sh`: production launcher entrypoint.
- Do not claim production boot or memory behavior is active unless runtime
  events, startup context, or database observation proves it.

## Repo Operations
- Inspect before modifying; prefer small, reviewable patches.
- Do not touch secrets, `.env` files, credentials, tokens, cookies, or API keys.
  Redact tokens, API keys, cookies, passwords, and approval tokens as
  `REDACTED`.
- Do not edit memory files unless the task explicitly requires it.
- Do not delete or overwrite memory files.
- Do not commit unless Hector explicitly asks.
- Never use `git add .`.
- In a dirty working tree, close isolated PRs with an exact manifest and
  staged-only validation in a temporary worktree before committing.

## Engineering Workflow
- For non-trivial changes, define goal, scope, expected files, acceptance
  criteria, and verification plan before implementation.
- Define the verification plan before mutating files.
- Work in small checkpoints that can be reviewed and verified independently.
- Read-only triage is allowed unless the user forbids it.
- Edits, staging, commits, installs, migrations, deploys, deletes, and external
  mutations require explicit written approval.
- Do not mix fronts without explicit approval: F2 RuntimeDb/durability, F3
  leases, F4 Browser, F5 brief/task, docs/memory.
- Do not activate `CLAW_FORMAL_JOB_LEASES_ENABLED` until runners propagate
  `lease_owner` + `lease_generation` and heartbeat works end-to-end.

## Anti-Patterns
- Stale memory treated as current evidence.
- Defeatist durable conclusions encoded into memory, facts, lessons, prompts, or
  state.
- Blind retry loops that repeat the same action without changing strategy.
- Prompt-only fixes for state-machine, routing, timeout, selector, persistence,
  or retry bugs.
- Silent fallback paths that hide broken dispatch, persistence, browser, or
  computer-use behavior.

## Review Priorities
When reviewing pull requests, prioritize correctness, security, regressions,
data integrity, API contract consistency, migration safety, permissions,
observability, and missing tests. Flag P0, P1, and P2 issues with concrete
evidence and suggested fixes.

Do not focus on formatting or subjective style unless it creates a real
maintainability, security, or correctness issue.

Treat the following as high priority:
- Authentication or authorization regressions.
- PII logging.
- Unsafe input handling.
- Database migration risks.
- API contract mismatches.
- Frontend/backend schema mismatches.
- Missing tests for critical flows.
- Changes that can break existing consumers.
- Environment or configuration changes without documentation.
- Silent error handling.
- Data consistency risks.
- Race conditions.
- Broken retry semantics.
- Stale memory contamination.
- Production/local divergence.

## Verification
- Focused boot/context check: `.venv/bin/python -m pytest tests/test_workspace.py tests/test_lifecycle.py -q`.
- Focused runtime prompt check: `.venv/bin/python -m pytest tests/test_brain_core.py tests/test_memory_core.py -q`.
- Full suite when needed: `.venv/bin/python -m pytest tests/ -q`.
- Manual boot observability: inspect `observe_stream` for
  `agent_startup_context` after restart.
- Production restart, when explicitly approved: `./scripts/restart.sh`.
- If a real Telegram test contradicts local tests, first verify the live PID,
  cwd, launchd label, branch, untracked boot files, and daemon route before
  editing personality files.
- Do not consider boot/memory work resolved until a post-restart
  `agent_startup_context` event exists in production `data/claw.db`.
- Do not say `BOOT_PROTOCOL.md` is loaded unless runtime events, startup
  context, or database observation proves it for the live daemon.

## Output Contract
At the end of each Codex task, report:
- Summary.
- Files changed.
- Tests/checks run.
- Result.
- Risks/unknowns.
- Next concrete action if blocked.

If no files changed, say so. If verification was not run, say exactly why.
