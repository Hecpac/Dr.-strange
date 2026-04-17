# Claw v2.1 — Pending Items Completion Design

**Date:** 2026-03-23
**Status:** Approved (rev 3 — addresses threading, launchd, wiring, and scope findings)
**Scope:** Complete the remaining unimplemented items from the Claw v2.1.6 PRD

---

## 1. Context

Claw v2.1 has ~3,864 lines across 20 modules + 3 adapters. The core runtime (LLM routing, brain, agents, bot service, memory, tools, sandbox, sanitizer, eval, observability, approval) is fully implemented and passing 469 tests.

Five categories of work remain:

1. Context markdown files (SOUL.md, HEARTBEAT.md, CRON.md, USER.md, SECURITY.md, AGENTS.md)
2. Telegram transport layer (actual python-telegram-bot integration)
3. Voice module (Whisper STT only; TTS-1 utility provided but not wired — see Non-Goals)
4. main.py restructuring (extract lifecycle, add PID lock, signal handlers, async event loop)
5. daemon.py completion (async run loop, launchd plist)

### Non-Goals (this iteration)

- **TTS outbound replies** — `voice.synthesize()` is implemented as a utility but NOT wired into Telegram replies. Future: toggle via `/voice` command.
- **HEARTBEAT.md-driven checks** — The file is created; wiring `HeartbeatService` to read and execute its checklist items is deferred to the heartbeat hardening phase.
- **SECURITY.md enforcement** — The file is created; wiring `SandboxPolicy`/`DomainAllowlistEnforcer` to load allowlists from it is deferred to the security hardening phase.
- **USER.md in prompt prefix** — The file is created; including it in the cached prompt prefix alongside SOUL.md is deferred to the prompt caching phase.
- **AGENTS.md auto-update** — The file is created; `AutoResearchAgentService` writing metrics into it is deferred.
- **bot.py refactor** — Already at 414 lines (exceeds 250-line limit). Out of scope; flagged as tech debt.

---

## 2. Approach

**Single asyncio loop architecture.** One process runs telegram polling + periodic daemon ticks in the same event loop. One launchd plist, one PID lock.

**Threading strategy:** `BotService.handle_text()` is synchronous and calls `ClaudeSDKExecutor`, which internally runs `asyncio.run()` (cannot nest inside the main loop). It must be offloaded via `asyncio.to_thread()`. To make this safe, SQLite connections in `memory.py` and `observe.py` are opened with `check_same_thread=False`. This is safe because: (1) Python's GIL serializes SQLite operations, (2) SQLite itself handles file-level locking, (3) only one thread runs a blocking request at a time while the main loop handles polling/ticks.

**Telegram library:** `python-telegram-bot` v20+ (async, mature, user's choice).

**Voice:** Fresh build using OpenAI SDK (`whisper-1` for STT, `tts-1` for TTS utility).

---

## 3. Deviations from PRD

| Deviation | Justification |
|-----------|---------------|
| New `telegram.py` not in PRD file structure | PRD puts "Telegram I/O" in `bot.py`, but `bot.py` is already a 414-line service layer (over 250-line limit). Separating async transport from synchronous command dispatch keeps both files under limit and preserves `bot.py`'s PROTECTED status. |
| New `lifecycle.py` not in PRD file structure | `main.py` is already 182 lines. Adding PID lock, signal handlers, and async run loop would exceed 250 lines. Extract lifecycle concerns into dedicated module. |
| `check_same_thread=False` on SQLite connections | Required for `asyncio.to_thread()` bridge. Safe under GIL + SQLite locking. Minimal change to `memory.py` and `observe.py`. |

---

## 4. Design

### 4.1 Context Markdown Files

Six files created in `claw_v2/` with content from PRD sections 6.1–6.5:

| File | PRD Source | Editable by | Wired into code? |
|------|-----------|-------------|-------------------|
| `SOUL.md` | PRD 6.1 (verbatim) | Human only | Yes — loaded as system prompt in `lifecycle.py` |
| `HEARTBEAT.md` | PRD 6.2 (verbatim) | Human only | No — created only; integration deferred |
| `CRON.md` | PRD 6.3 (verbatim) | Human only | No — created only; integration deferred |
| `USER.md` | PRD 6.4 (verbatim) | Human only | No — created only; prompt prefix integration deferred |
| `SECURITY.md` | PRD 6.5 (verbatim) | Human only | No — created only; enforcement integration deferred |
| `AGENTS.md` | New template | Claw updates metrics | No — created only; auto-update deferred |

`AGENTS.md` template:
```markdown
# Claw — Agent Registry

| Agent | Class | Trust | Status | Last Metric | Last Run |
|-------|-------|-------|--------|-------------|----------|
<!-- Claw updates this table automatically. -->
```

`lifecycle.py` loads `SOUL.md` as the system prompt:
```python
soul_path = Path(__file__).parent / "SOUL.md"
system_prompt = soul_path.read_text() if soul_path.exists() else "You are Claw."
```

### 4.2 telegram.py — Transport Layer (~80 lines)

New file `claw_v2/telegram.py`. Thin async wrapper around `python-telegram-bot` v20.

```python
class TelegramTransport:
    def __init__(self, bot_service: BotService, token: str, allowed_user_id: str | None = None,
                 voice_api_key: str | None = None):
        ...

    async def start(self) -> None:
        """Build Application, register handlers, start non-blocking polling.
        Uses application.initialize() + application.start() + application.updater.start_polling()
        (NOT run_polling() which blocks the event loop)."""

    async def stop(self) -> None:
        """application.updater.stop() + application.stop() + application.shutdown()."""

    async def _handle_text(self, update, context) -> None:
        """Filters by allowed_user_id (silently drops unauthorized).
        Wraps BotService.handle_text() in asyncio.to_thread() since ClaudeSDKExecutor
        internally calls asyncio.run() which cannot nest in the main event loop.
        Splits responses at 4096 chars."""

    async def _handle_voice(self, update, context) -> None:
        """Download .ogg → voice.transcribe() → BotService.handle_text() → reply text."""
```

Key decisions:
- **Non-blocking polling:** Uses `initialize()` + `start()` + `updater.start_polling()` (not `run_polling()`)
- **Sync/async bridge:** `BotService.handle_text()` offloaded via `asyncio.to_thread()`. SQLite connections use `check_same_thread=False` to support this.
- **Telegram is optional:** If `token` is `None`, `start()` is a no-op and returns immediately. Lifecycle skips transport setup when `TELEGRAM_BOT_TOKEN` is unset. Existing tests that build runtime without a token continue to work.
- Filters by `allowed_user_id` at transport level (intentional duplication with BotService — defense in depth; transport silently drops, BotService raises PermissionError as second layer)
- Long responses split at 4096 chars (Telegram limit)
- Errors caught and sent back as text, never crash the bot

### 4.3 voice.py — Whisper STT + TTS-1 Utility (~80 lines)

New file `claw_v2/voice.py`. Two async functions, no state.

```python
class VoiceUnavailableError(RuntimeError):
    """Raised when voice services cannot be used (missing API key)."""

async def transcribe(audio_path: Path, *, api_key: str | None = None) -> str:
    """OGG/MP3/WAV → text via OpenAI Whisper API (whisper-1).
    Raises VoiceUnavailableError if no API key."""

async def synthesize(text: str, *, api_key: str | None = None, voice: str = "alloy") -> Path:
    """Text → MP3 temp file via OpenAI TTS-1 API. Caller cleans up.
    Raises VoiceUnavailableError if no API key.
    NOTE: Not wired into Telegram replies in this iteration."""
```

- Uses `openai` SDK (already in dependencies)
- Defines own `VoiceUnavailableError` (avoids cross-boundary import from `adapters.base`)
- `transcribe` accepts raw file path from Telegram download
- `synthesize` writes to `tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)`
- Models hardcoded: `whisper-1`, `tts-1`

### 4.4 lifecycle.py — Process Lifecycle (~80 lines)

New file `claw_v2/lifecycle.py`. Extracted from main.py to stay under 250-line limit.

```python
class PidLock:
    """Acquire/release ~/.claw/claw.pid. Check if existing PID is alive via os.kill(pid, 0)."""
    def __init__(self, path: Path): ...
    def acquire(self) -> None: ...
    def release(self) -> None: ...

async def run(*, system_prompt: str | None = None) -> int:
    """Main async entry point.
    1. Acquire PID lock
    2. Build runtime via main.build_runtime(system_prompt=load_soul())
    3. Register signal handlers:
       - SIGTERM/SIGINT → shutdown.set() for graceful stop
       - SIGHUP → shutdown.set() (launchd KeepAlive restarts = effective reload)
    4. Start concurrent tasks (only those available):
       - TelegramTransport (skipped if TELEGRAM_BOT_TOKEN is unset)
       - ClawDaemon.run_loop (always runs)
    5. await shutdown event
    6. Graceful teardown: stop telegram, release PID lock
    """
```

**SIGHUP reload strategy:** SIGHUP sets the shutdown event, causing graceful exit. Launchd's `KeepAlive: true` restarts the process automatically, effectively reloading all modules. This is simpler and safer than hot-reloading Python modules in-process.

### 4.5 main.py — Slim Entry Point (~185 lines total)

`main.py` keeps `build_runtime()` (unchanged, ~100 lines). The `main()` function becomes a thin wrapper:

```python
def main() -> int:
    return asyncio.run(lifecycle.run())
```

Total: existing `build_runtime` (~100 lines) + imports + `main()` wrapper = ~185 lines, under 250.

### 4.6 daemon.py — Run Loop (~55 lines total)

Add `run_loop` to existing `ClawDaemon`. **Remove the unconditional `heartbeat.emit()` from `tick()`** — heartbeat is already registered as a `ScheduledJob` in `CronScheduler` (see `main.py:138`), so `tick()` calling `heartbeat.emit()` directly causes duplicate heartbeats. `tick()` should only run `scheduler.run_due()` and emit the observability event.

```python
def tick(self, *, now: float | None = None) -> TickResult:
    """Run due scheduled jobs. Heartbeat runs via CronScheduler, not directly."""
    executed_jobs = self.scheduler.run_due(now=now)
    snapshot = self.heartbeat.collect()  # collect only, don't emit (scheduler handles that)
    if self.observe is not None:
        self.observe.emit("daemon_tick", payload={"executed_jobs": executed_jobs, ...})
    return TickResult(executed_jobs=executed_jobs, heartbeat=snapshot)

async def run_loop(self, shutdown: asyncio.Event, interval: float = 60.0) -> None:
    """Periodic tick loop. Ticks every 60s; CronScheduler tracks per-job intervals."""
    while not shutdown.is_set():
        try:
            self.tick()
        except Exception as exc:
            if self.observe:
                self.observe.emit("daemon_tick_error", payload={"error": str(exc)})
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass  # timeout elapsed = next tick
```

### 4.7 SQLite Thread Safety (~2 lines changed)

Add `check_same_thread=False` to existing SQLite connections:

- `memory.py:46` — `sqlite3.connect(self.db_path)` → `sqlite3.connect(self.db_path, check_same_thread=False)`
- `observe.py:25` — same change

This is the minimal change needed to support `asyncio.to_thread()` in the Telegram transport.

### 4.8 ops/ — Launchd Integration

**`ops/com.pachano.claw.plist`:**
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "...">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.pachano.claw</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/hector/Projects/Dr.-strange/ops/claw-launcher.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/hector/.claw/claw.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/hector/.claw/claw.stderr.log</string>
</dict>
</plist>
```

All paths are **absolute** — launchd does not expand `~`. No `WorkingDirectory` needed; the launcher script handles it.

**`ops/claw-launcher.sh`:**
```bash
#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$HOME/.claw/env" 2>/dev/null || true
cd "$REPO_ROOT"
exec "$REPO_ROOT/.venv/bin/python" -m claw_v2.main
```

Key: uses the repo's `.venv/bin/python` (not system `python3`) and `cd`s to the repo root so `claw_v2` is importable.

**`~/.claw/env` expected contents (created manually by user):**
```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_ALLOWED_USER_ID="..."
export OPENAI_API_KEY="..."           # optional, for voice + verifier/judge lanes
export GOOGLE_API_KEY="..."           # optional, for research lane
export APPROVAL_SECRET="..."         # HMAC secret for approval tokens
# Any other overrides (BRAIN_MODEL, WORKER_MODEL, etc.)
```

### 4.9 pyproject.toml

Add `python-telegram-bot` to dependencies:
```toml
dependencies = ["claude-agent-sdk", "openai", "google-genai", "python-telegram-bot"]
```

---

## 5. Test Plan

| Test file | What it covers |
|-----------|---------------|
| `test_telegram.py` (~100 lines) | Mock Application; text/voice routing to BotService; user filtering; message splitting; to_thread bridging; optional token (no-op start) |
| `test_voice.py` (~80 lines) | Mock openai client; transcribe/synthesize calls; error handling; missing API key raises VoiceUnavailableError |
| `test_lifecycle.py` (~80 lines) | PID lock acquire/release; signal handling; SOUL.md loading; graceful shutdown; optional Telegram |
| `test_daemon.py` (~60 lines) | run_loop ticks on schedule; error resilience (exception bound correctly); stops on shutdown event; no duplicate heartbeats |

All tests use mocks — no real API calls or Telegram connections. Test naming follows existing `test_<module>.py` convention.

---

## 6. Files Summary

| Action | File | Lines (est.) |
|--------|------|-------------|
| Create | `claw_v2/SOUL.md` | ~35 |
| Create | `claw_v2/HEARTBEAT.md` | ~12 |
| Create | `claw_v2/CRON.md` | ~12 |
| Create | `claw_v2/USER.md` | ~8 |
| Create | `claw_v2/SECURITY.md` | ~35 |
| Create | `claw_v2/AGENTS.md` | ~6 |
| Create | `claw_v2/telegram.py` | ~80 |
| Create | `claw_v2/voice.py` | ~80 |
| Create | `claw_v2/lifecycle.py` | ~80 |
| Edit | `claw_v2/main.py` | ~185 total (trim main()) |
| Edit | `claw_v2/daemon.py` | ~55 total |
| Edit | `claw_v2/memory.py` | +1 (check_same_thread) |
| Edit | `claw_v2/observe.py` | +1 (check_same_thread) |
| Create | `ops/claw-launcher.sh` | ~12 |
| Create | `ops/com.pachano.claw.plist` | ~25 |
| Edit | `pyproject.toml` | +1 dep |
| Create | `tests/test_telegram.py` | ~100 |
| Create | `tests/test_voice.py` | ~80 |
| Create | `tests/test_lifecycle.py` | ~80 |
| Create | `tests/test_daemon.py` | ~60 |

---

## 7. Constraints

- Every Python file stays under 250 lines (PRD hard constraint)
- No new dependencies beyond `python-telegram-bot`
- All existing 469 tests must continue passing
- Context .md files use PRD verbatim content — no embellishment
- SQLite `check_same_thread=False` is the only change to existing modules beyond main.py/daemon.py
