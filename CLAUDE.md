# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`claw_v2/` is **Claw v2 / Dr. Strange**: a multi-LLM autonomous agent daemon that
runs 24/7, takes input from Telegram / web chat / cron / CLI, and executes
authorized work through gated tools and sub-agents. Python 3.13, `uv`-managed.
The rest of the repo root is the agent's *home* (memory, reports, artifacts,
adjacent projects) — not application code.

## Commands

Use the project venv directly; `uv` manages dependencies.

```bash
# Run the daemon locally (foreground)
.venv/bin/python -m claw_v2.main
# Production restart (launchd-managed daemon)
./scripts/restart.sh

# Tests — full suite (~6 min, ~2800 tests + subtests)
.venv/bin/python -m pytest tests/ -q
# Single file / class / method
.venv/bin/python -m pytest tests/test_brain_core.py -q
.venv/bin/python -m pytest tests/test_approval.py::ApprovalManagerTests -q
.venv/bin/python -m pytest "tests/test_config.py::AppConfigDefaultsTests::test_approval_ttl_defaults_to_900_and_accepts_override" -q
# Focused boot/context check
.venv/bin/python -m pytest tests/test_workspace.py tests/test_lifecycle.py -q

# Lint / format / typecheck (same tooling the self-improve promotion gate runs)
uvx ruff check claw_v2 tests
uvx ruff format --check claw_v2 tests
uvx mypy <changed_files>            # advisory only — does not gate

# Observability — "see how it thinks" without opening SQLite
python -m claw_v2.cli.think tail --limit 20            # latest events
python -m claw_v2.cli.think tail --type dispatch_decision
python -m claw_v2.cli.think trace <trace_id>
python -m claw_v2.cli.think replay <session_id>        # session reasoning chain
python -m claw_v2.cli.think spending                   # cost rollup today
python -m claw_v2.cli.think circuit                    # observation-window state
```

There is no Makefile and no console-script entry point; the module forms above
are canonical. The daemon does **not** need to be running for the `think` CLI.

`tests/test_architecture_invariants.py` is a tripwire suite: it AST-scans
runtime code to enforce the invariants in §1 of `INTERNAL_WIRING.md` (no async
subprocess exec / `shell=True` / inline heavy work in `daemon.tick`, critical
floors on self-improve promotion, etc.). A failure there means a structural
invariant was broken, not just a logic bug — read the named invariant before
"fixing" the test.

## Architecture (big picture)

Read `claw_v2/INTERNAL_WIRING.md` before touching dispatchers
(`bot.py:handle_text`), brain/verifier (`brain.py`), `AgentLoop`,
`ToolRegistry`, or lanes. It is the source of truth for invariants (§1),
prescriptive `do_not` rules (§6), the 15-handler dispatch order (§5.1), and open
TODOs (§7). This section is the map; that doc is the territory.

**Inbound message flow** (`BotService.handle_text`, `claw_v2/bot.py`):

```
channel → 15 pre-brain dispatchers (§5.1)   # capture only when target is
   ↓        unambiguous from literal text                 unambiguous; else fall through
   CapabilityRouter → CapabilityPreflight    # intent→route; binaries+sandbox preflight
   ↓
   BrainService → LLMRouter.ask(lane="brain")
   ↓        (CircuitBreaker, anthropic↔openai fallback, ObservationWindow gate)
   tool calls → ToolRegistry.execute         # triple-AND gating (below)
   ↓
   heavy work → TaskHandler → CoordinatorService (research→synthesis→impl→verify)
                wrapped by AgentLoop (plan/execute/observe/verify/critique/replan)
```

Default route for every message is the **brain**. Pre-brain dispatchers are
exceptions; conversational continuations ("continúa", "procede", numbered
picks, quoted replies) MUST fall through to the brain, which has the session
state to resolve them (see `AGENTS.md` Routing Contract).

**Triple-AND tool gating** — the core safety invariant. A tool runs only when
*all three* independent authorizations pass (single-flag bypass is impossible
by construction):
1. `allowed_agent_classes` — which sub-agent may see the tool
2. `ToolPolicy.allowed_contexts` — from where it may be invoked
3. tier check — Tier 1/2 auto-execute; **Tier 3 always** hits the approval gate
   (`autoexec_max_tier` is a ceiling, never an override)

`ToolRegistry` lives in `claw_v2/tools.py`; policies are data-driven from
`claw_v2/config/tool_policies.json` (loaded fail-fast at import). Secret-path
denylists stay code-owned via the `SECRET_PATH_PATTERNS` sentinel so a JSON edit
cannot weaken them.

**LLM lanes** (`LLMRouter`, `claw_v2/llm.py`): `brain` / `worker` /
`worker_heavy` are tool-capable; `verifier` / `research` / `judge` are
advisory, read-only, and must **never** be granted tool access (enforced by
`_validate_lane_input`). Codex has no fallback provider (it's a ChatGPT
subscription) — failures defer, never silently switch providers.

**Approval & autonomy**: `ApprovalManager` (`claw_v2/approval.py`) is the single
source of truth for approvals — file-backed, HMAC-token, fcntl-locked; states
`pending→approved/rejected/expired/archived`. The gate is selected by a
ContextVar: Telegram gate by default (raises `ApprovalPending`, user runs
`/approve <id> <token>`), system auto-approve gate inside `system_approval_mode`
(daemon, Kairos, heartbeat). Kairos handlers that mutate external state
(social/deploy) must create a pending record or be opt-in via an explicit env
flag — they never call adapters directly.

**Scheduler discipline**: `CronScheduler.run_due()` runs handlers synchronously,
so heavy/LLM/subprocess jobs must enqueue durable `agent_jobs` and execute in a
`ClawDaemon` background runner **off-tick** — never inline in `daemon.tick()`
(Core Invariant 1, AST-enforced). All runtime subprocess execution goes through
`subprocess_runner.run_subprocess_bounded[_off_loop]` (timeout + process-group
kill + bounded output); no `shell=True`, `os.system`, or ad-hoc
`create_subprocess_exec` in runtime code.

**Where state lives**:
- `data/claw.db` — SQLite: messages, session_state, facts, lessons, task_ledger,
  cron state, and the `observe_stream` event log (every decision emits an event;
  this is the audit-trail invariant).
- `data/observation_window.json` — circuit/budget freeze state (survives restart).
- `~/.claw/scratch/<task_id>/` — coordinator phase artifacts (resumable).
- `~/.claw/env` — daemon environment (sourced by launcher; secrets, feature flags).
- Sub-agent definitions (Alma, Hex, Lux, Rook, …) are discovered by scanning for
  `SOUL.md` / `HEARTBEAT.md` / `USER.md` directories (`claw_v2/agents.py`).

**Config & flags**: `AppConfig` (`claw_v2/config.py`) reads environment.
Behavior-shaping flags worth knowing: `CLAW_DISABLE_TASK_INTENT_ROUTER` (default
on), `CLAW_COMPUTER_AUTO_APPROVE`, `KAIROS_AUTO_PUBLISH_SOCIAL` /
`KAIROS_AUTO_DEPLOY`, `BRAIN_TOOLUSE_VERIFY`,
`CLAW_PENDING_VERIFICATION_DRAIN_APPLY`, `APPROVAL_TTL_SECONDS` (default 900),
`CLAUDE_AUTH_MODE`.

After any change that moves a symbol named in `INTERNAL_WIRING.md`, update its
`describes_commit` and `last_verified` in the **same commit**.

---

# Behavioral guidelines (Dr. Strange / Claw)

Derived from Karpathy's LLM coding observations.

## 1. Think Before Coding

- State assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.

## 2. Simplicity First

- No features beyond what was asked.
- No abstractions for single-use code.
- No error handling for impossible scenarios.
- If 200 lines could be 50, rewrite it.

## 3. Surgical Changes

- Don't "improve" adjacent code, comments, or formatting.
- Match existing style, even if you'd do it differently.
- Remove only imports/variables/functions that YOUR changes made unused.
- Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

Transform tasks into verifiable goals:

```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

## 5. Worktrees

Worktrees ubicados en `.worktrees/` (local al proyecto). No commitear contenido de ese directorio.

## 6. Internal Wiring reference

Antes de refactorear dispatchers (`bot.py:handle_text`), brain/verifier
(`brain.py`), AgentLoop, ToolRegistry, o lanes — leer
`claw_v2/INTERNAL_WIRING.md`. Cataloga invariantes (§1), reglas `do_not`
(§6), el orden canónico de los 15 dispatch handlers (§5.1), y los TODOs
abiertos por ola (§7). Tras un cambio que toque algo descrito ahí, actualizar
`describes_commit` y `last_verified` en el mismo commit.

Para "ver cómo piensa" sin abrir SQLite: `python -m claw_v2.cli.think
tail|trace|spending|circuit|replay`.
