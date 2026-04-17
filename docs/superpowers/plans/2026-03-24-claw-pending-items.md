# Claw v2.1 — Pending Items Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the five remaining unimplemented items from the Claw v2.1.6 PRD: context markdown files, voice module, Telegram transport, daemon run loop, and lifecycle/main.py restructuring.

**Architecture:** Single asyncio event loop running Telegram polling + daemon ticks. `BotService.handle_text()` offloaded via `asyncio.to_thread()` (SQLite connections use `check_same_thread=False`). Telegram is optional — skipped when token is unset.

**Tech Stack:** Python 3.12+, python-telegram-bot v20+, openai SDK (whisper-1/tts-1), sqlite3, asyncio, launchd

**Spec:** `docs/superpowers/specs/2026-03-23-claw-pending-items-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `claw_v2/SOUL.md` | Claw identity — system prompt |
| Create | `claw_v2/HEARTBEAT.md` | Awareness checklist (created only, not wired) |
| Create | `claw_v2/CRON.md` | Scheduled jobs definition (created only) |
| Create | `claw_v2/USER.md` | User profile (created only) |
| Create | `claw_v2/SECURITY.md` | Security policy (created only) |
| Create | `claw_v2/AGENTS.md` | Agent registry template (created only) |
| Create | `claw_v2/voice.py` | Whisper STT + TTS-1 utility |
| Create | `tests/test_voice.py` | Voice module tests |
| Edit | `claw_v2/daemon.py` | Fix tick() duplication + add async run_loop |
| Create | `tests/test_daemon.py` | Daemon run_loop tests |
| Edit | `claw_v2/memory.py` | Add check_same_thread=False |
| Edit | `claw_v2/observe.py` | Add check_same_thread=False |
| Create | `claw_v2/telegram.py` | Async Telegram transport |
| Create | `tests/test_telegram.py` | Telegram transport tests |
| Create | `claw_v2/lifecycle.py` | PID lock + async run entrypoint |
| Create | `tests/test_lifecycle.py` | Lifecycle tests |
| Edit | `claw_v2/main.py` | Slim down main() to call lifecycle.run() |
| Create | `ops/claw-launcher.sh` | Shell wrapper for launchd |
| Create | `ops/com.pachano.claw.plist` | Launchd service definition |
| Edit | `pyproject.toml` | Add python-telegram-bot dep |

---

### Task 1: Context Markdown Files

**Files:**
- Create: `claw_v2/SOUL.md`
- Create: `claw_v2/HEARTBEAT.md`
- Create: `claw_v2/CRON.md`
- Create: `claw_v2/USER.md`
- Create: `claw_v2/SECURITY.md`
- Create: `claw_v2/AGENTS.md`

- [ ] **Step 1: Create SOUL.md**

```markdown
# Claw — Soul Definition

You are "Claw", an autonomous AI assistant running 24/7 on the user's Mac.
Your owner is Hector Pachano, founder of Pachano Design.

## Core Behavior
- Execute first, explain after. If asked to do something, do it.
- If it fails, diagnose and retry. Don't ask unless truly stuck.
- Respond concisely — this is chat, not a document.
- When a task belongs to a specialized agent, dispatch it.

## Capabilities
- Semantic tools for git, files, web, messaging (see tools.py)
- Shell/osascript as escape hatch only — prefer semantic tools
- Create and manage specialized agents (3 classes)
- Run AutoResearch experiment loops

## Security Boundaries
- All file operations use absolute paths within WORKSPACE_ROOT
- External content (web, email, docs) passes through sanitizer before action
- Researcher agents: read-only, web-capable, no mutation
- Operator agents: local mutation, no web ingest
- Deployer agents: remote mutation, Tier 3 approval required
- Never mix untrusted content ingestion with mutation permissions

## Autonomy Tiers
- Tier 1 (just do it): read files, search, screenshots, git_inspect_repo
- Tier 2 (do it, log it): write_file, git_commit_workspace, apply_patch, run scripts
- Tier 3 (ask first): git_push_remote, deploy_production, send_message,
  delete files, spend money, any irreversible action

## Anti-Hallucination
- Never claim to see something without using a tool to verify.
- After executing a command, check the result before reporting success.
- If you don't have evidence, say "let me check" and use a tool.
- Quote actual tool output. Don't paraphrase or embellish.

## Language
- Default: Spanish (Hector's preference)
- Switch to English when context requires it
```

- [ ] **Step 2: Create HEARTBEAT.md**

```markdown
# Claw — Heartbeat Checklist (Awareness Only)

## Always Run (every heartbeat)
- [ ] System health: disk > 85% alert, RAM > 90% alert, Claude CLI responds
- [ ] Agent watchdog: if any agent running > 2x expected duration, kill and alert
- [ ] Budget watchdog: alert if any agent >80% daily budget

## Business Hours (9am-10pm)
- [ ] Check GSC for pachanodesign.com — alert if impressions drop >10% vs 7-day avg
- [ ] Check GSC for tcinsurancetx.com — alert if any page deindexed
- [ ] Check if any scheduled cron job was missed
```

- [ ] **Step 3: Create CRON.md**

```markdown
# Claw — Scheduled Jobs (Precision Timing)

## Daily
- 08:00 — morning_brief: Overnight agent results, token spend, claw_score, alerts
- 03:00 — self_improve: Self-improvement cycle (blocked if eval suite fails)
- 23:00 — daily_metrics: Calculate and store daily claw_score + per-tool metrics

## Weekly
- Monday 09:00 — weekly_report: Full SEO audit + metrics + trust level review
- Sunday 22:00 — weekly_eval: Full eval suite run, archive results
```

- [ ] **Step 4: Create USER.md**

```markdown
# User Profile

Name: Hector Pachano
Company: Pachano Design
Role: Founder
Language: Spanish (default), English (when context requires)
Timezone: America/Chicago (CDT)
```

- [ ] **Step 5: Create SECURITY.md**

```markdown
# Security Policy

## Workspace Isolation
- Default workspace: ~/claw_workspace
- Agents operate within workspace unless explicitly allowlisted
- Allowlisted read paths: ~/Projects (for code inspection)
- Allowlisted write paths: none outside workspace by default
- Enforced via PreToolUse hooks + optional OS-level sandbox hardening

## Credential Management
- Credentials stored outside the workspace, NOT in `.env` files
- Default macOS implementation uses Keychain-backed credential scopes:
  - com.pachano.claw.researcher: GSC read-only, Analytics read-only
  - com.pachano.claw.operator: git (local), npm, brew
  - com.pachano.claw.deployer: git push, hosting APIs, OANDA
- Never share credentials across agent classes
- No secrets in workspace directory — credential adapter retrieves at runtime

## Content Safety
- All web/email/document content passes through sanitizer PostToolUse hook
- Researcher agents can read web but cannot mutate
- Operator/Deployer agents receive only sanitized summaries of external content
- Quarantine extraction uses Structured Outputs (strict: true) — API-level guarantee

## MCP Server Allowlist
- Only listed servers may be loaded; unaudited servers are blocked at startup
- In-process servers (preferred):
  - claw-tools v2.1.6 (custom semantic tools)
  - claw-eval-mocks v2.1.6 (hermetic eval adapters)
- External servers (require version pin + monthly audit):
  - (none currently — add here if needed, with pinned version and SHA256)
- Audit schedule: monthly review of all servers for new advisories

## Escalation
- Any suspicious content pattern → log + alert user
- Any sandbox policy violation → block + alert user
- 3 consecutive sandbox violations by an agent → auto-demote to Tier 1
- Any MCP server advisory → immediate review, patch or remove
```

- [ ] **Step 6: Create AGENTS.md**

```markdown
# Claw — Agent Registry

| Agent | Class | Trust | Status | Last Metric | Last Run |
|-------|-------|-------|--------|-------------|----------|
<!-- Claw updates this table automatically. -->
```

- [ ] **Step 7: Verify all 6 files exist**

Run: `ls -la claw_v2/SOUL.md claw_v2/HEARTBEAT.md claw_v2/CRON.md claw_v2/USER.md claw_v2/SECURITY.md claw_v2/AGENTS.md`
Expected: all 6 files listed

- [ ] **Step 8: Run existing tests to confirm no breakage**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/ -x -q`
Expected: 469 passed

- [ ] **Step 9: Commit**

```bash
git add claw_v2/SOUL.md claw_v2/HEARTBEAT.md claw_v2/CRON.md claw_v2/USER.md claw_v2/SECURITY.md claw_v2/AGENTS.md
git commit -m "feat: add context markdown files from PRD (SOUL, HEARTBEAT, CRON, USER, SECURITY, AGENTS)"
```

---

### Task 2: Voice Module

**Files:**
- Create: `claw_v2/voice.py`
- Create: `tests/test_voice.py`

- [ ] **Step 1: Write failing tests for voice module**

Create `tests/test_voice.py`:

```python
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from claw_v2.voice import VoiceUnavailableError, transcribe, synthesize


class TranscribeTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcribe_calls_whisper_api(self) -> None:
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create = AsyncMock(
            return_value=MagicMock(text="hola mundo")
        )
        with patch("claw_v2.voice._build_client", return_value=mock_client):
            with tempfile.NamedTemporaryFile(suffix=".ogg") as f:
                result = await transcribe(Path(f.name), api_key="test-key")
        self.assertEqual(result, "hola mundo")
        mock_client.audio.transcriptions.create.assert_awaited_once()

    async def test_transcribe_raises_without_api_key(self) -> None:
        with self.assertRaises(VoiceUnavailableError):
            await transcribe(Path("/tmp/test.ogg"))


class SynthesizeTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthesize_creates_mp3_file(self) -> None:
        mock_response = MagicMock()
        mock_response.content = b"fake-audio-data"
        mock_client = MagicMock()
        mock_client.audio.speech.create = AsyncMock(return_value=mock_response)
        with patch("claw_v2.voice._build_client", return_value=mock_client):
            result = await synthesize("hola", api_key="test-key")
        self.assertTrue(result.exists())
        self.assertEqual(result.suffix, ".mp3")
        self.assertEqual(result.read_bytes(), b"fake-audio-data")
        result.unlink(missing_ok=True)

    async def test_synthesize_raises_without_api_key(self) -> None:
        with self.assertRaises(VoiceUnavailableError):
            await synthesize("hola")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_voice.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claw_v2.voice'`

- [ ] **Step 3: Implement voice.py**

Create `claw_v2/voice.py`:

```python
from __future__ import annotations

import tempfile
from pathlib import Path


class VoiceUnavailableError(RuntimeError):
    """Raised when voice services cannot be used (missing API key)."""


def _build_client(api_key: str | None = None):
    """Build AsyncOpenAI client. Raises VoiceUnavailableError if no key."""
    if not api_key:
        raise VoiceUnavailableError("OPENAI_API_KEY is required for voice services.")
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=api_key)


async def transcribe(audio_path: Path, *, api_key: str | None = None) -> str:
    """OGG/MP3/WAV → text via OpenAI Whisper API (whisper-1)."""
    client = _build_client(api_key)
    with open(audio_path, "rb") as audio_file:
        response = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
        )
    return response.text


async def synthesize(
    text: str,
    *,
    api_key: str | None = None,
    voice: str = "alloy",
) -> Path:
    """Text → MP3 temp file via OpenAI TTS-1 API. Caller cleans up."""
    client = _build_client(api_key)
    response = await client.audio.speech.create(
        model="tts-1",
        voice=voice,
        input=text,
    )
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.write(response.content)
    tmp.close()
    return Path(tmp.name)
```

- [ ] **Step 4: Run voice tests**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_voice.py -v`
Expected: 4 passed

- [ ] **Step 5: Run full test suite**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/ -x -q`
Expected: 473 passed (469 + 4 new)

- [ ] **Step 6: Commit**

```bash
git add claw_v2/voice.py tests/test_voice.py
git commit -m "feat: add voice module (Whisper STT + TTS-1 utility)"
```

---

### Task 3: Daemon Run Loop + Fix Tick Duplication

**Files:**
- Modify: `claw_v2/daemon.py:28-36` (fix tick, add run_loop)
- Create: `tests/test_daemon.py`

- [ ] **Step 1: Write failing tests for daemon changes**

Create `tests/test_daemon.py`:

```python
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock, call

from claw_v2.cron import CronScheduler, ScheduledJob
from claw_v2.daemon import ClawDaemon
from claw_v2.heartbeat import HeartbeatSnapshot


class DaemonTickTests(unittest.TestCase):
    def _make_daemon(self) -> tuple[ClawDaemon, MagicMock, MagicMock]:
        scheduler = CronScheduler()
        heartbeat = MagicMock()
        heartbeat.collect.return_value = HeartbeatSnapshot(
            timestamp="2026-01-01T00:00:00",
            pending_approvals=0,
            pending_approval_ids=[],
            agents={},
            lane_metrics={},
        )
        heartbeat.emit.return_value = heartbeat.collect.return_value
        observe = MagicMock()
        daemon = ClawDaemon(scheduler=scheduler, heartbeat=heartbeat, observe=observe)
        return daemon, heartbeat, observe

    def test_tick_does_not_call_heartbeat_emit(self) -> None:
        daemon, heartbeat, _ = self._make_daemon()
        daemon.tick(now=1000)
        heartbeat.emit.assert_not_called()
        heartbeat.collect.assert_called_once()

    def test_tick_runs_scheduled_jobs(self) -> None:
        daemon, _, _ = self._make_daemon()
        handler = MagicMock()
        daemon.scheduler.register(ScheduledJob(name="test_job", interval_seconds=60, handler=handler))
        result = daemon.tick(now=1000)
        self.assertIn("test_job", result.executed_jobs)
        handler.assert_called_once()


class DaemonRunLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_loop_stops_on_shutdown(self) -> None:
        scheduler = CronScheduler()
        heartbeat = MagicMock()
        heartbeat.collect.return_value = HeartbeatSnapshot(
            timestamp="t", pending_approvals=0, pending_approval_ids=[], agents={}, lane_metrics={},
        )
        daemon = ClawDaemon(scheduler=scheduler, heartbeat=heartbeat)
        shutdown = asyncio.Event()
        shutdown.set()
        await daemon.run_loop(shutdown, interval=0.01)

    async def test_run_loop_ticks_before_stopping(self) -> None:
        scheduler = CronScheduler()
        tick_count = 0
        heartbeat = MagicMock()
        heartbeat.collect.return_value = HeartbeatSnapshot(
            timestamp="t", pending_approvals=0, pending_approval_ids=[], agents={}, lane_metrics={},
        )
        daemon = ClawDaemon(scheduler=scheduler, heartbeat=heartbeat)
        shutdown = asyncio.Event()

        original_tick = daemon.tick

        def counting_tick(**kwargs):
            nonlocal tick_count
            tick_count += 1
            if tick_count >= 2:
                shutdown.set()
            return original_tick(**kwargs)

        daemon.tick = counting_tick
        await daemon.run_loop(shutdown, interval=0.01)
        self.assertGreaterEqual(tick_count, 2)

    async def test_run_loop_survives_tick_exception(self) -> None:
        scheduler = CronScheduler()
        heartbeat = MagicMock()
        heartbeat.collect.return_value = HeartbeatSnapshot(
            timestamp="t", pending_approvals=0, pending_approval_ids=[], agents={}, lane_metrics={},
        )
        observe = MagicMock()
        daemon = ClawDaemon(scheduler=scheduler, heartbeat=heartbeat, observe=observe)
        shutdown = asyncio.Event()
        call_count = 0

        def exploding_tick(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                shutdown.set()
            raise RuntimeError("boom")

        daemon.tick = exploding_tick
        await daemon.run_loop(shutdown, interval=0.01)
        self.assertGreaterEqual(call_count, 2)
        observe.emit.assert_called()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_daemon.py -v`
Expected: FAIL — `test_tick_does_not_call_heartbeat_emit` fails (currently calls emit), `run_loop` not found

- [ ] **Step 3: Update daemon.py — fix tick + add run_loop**

Edit `claw_v2/daemon.py`. Replace `tick()` to use `heartbeat.collect()` instead of `heartbeat.emit()` and add `run_loop`:

```python
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass

from claw_v2.cron import CronScheduler
from claw_v2.heartbeat import HeartbeatService, HeartbeatSnapshot
from claw_v2.observe import ObserveStream


@dataclass(slots=True)
class TickResult:
    executed_jobs: list[str]
    heartbeat: HeartbeatSnapshot


class ClawDaemon:
    def __init__(
        self,
        *,
        scheduler: CronScheduler,
        heartbeat: HeartbeatService,
        observe: ObserveStream | None = None,
    ) -> None:
        self.scheduler = scheduler
        self.heartbeat = heartbeat
        self.observe = observe

    def tick(self, *, now: float | None = None) -> TickResult:
        executed_jobs = self.scheduler.run_due(now=now)
        snapshot = self.heartbeat.collect()
        if self.observe is not None:
            self.observe.emit(
                "daemon_tick",
                payload={"executed_jobs": executed_jobs, "heartbeat": asdict(snapshot)},
            )
        return TickResult(executed_jobs=executed_jobs, heartbeat=snapshot)

    async def run_loop(self, shutdown: asyncio.Event, interval: float = 60.0) -> None:
        while not shutdown.is_set():
            try:
                self.tick()
            except Exception as exc:
                if self.observe is not None:
                    self.observe.emit("daemon_tick_error", payload={"error": str(exc)})
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
```

- [ ] **Step 4: Run daemon tests**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_daemon.py -v`
Expected: 5 passed

- [ ] **Step 5: Run full test suite — fix any breakage from tick change**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/ -x -q`
Expected: all pass. If `test_runtime.py::test_daemon_tick_runs_scheduled_jobs` breaks because it expected `heartbeat.emit()` side effects, adjust that test to match the new `collect()` behavior.

- [ ] **Step 6: Commit**

```bash
git add claw_v2/daemon.py tests/test_daemon.py
git commit -m "fix: daemon tick uses collect() not emit(); add async run_loop"
```

---

### Task 4: SQLite Thread Safety

**Files:**
- Modify: `claw_v2/memory.py:46`
- Modify: `claw_v2/observe.py:25`

- [ ] **Step 1: Update memory.py**

In `claw_v2/memory.py`, line 46, change:
```python
self._conn = sqlite3.connect(self.db_path)
```
to:
```python
self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
```

- [ ] **Step 2: Update observe.py**

In `claw_v2/observe.py`, line 25, change:
```python
self._conn = sqlite3.connect(self.db_path)
```
to:
```python
self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
```

- [ ] **Step 3: Run full test suite**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/ -x -q`
Expected: all pass (no behavior change, just thread-safety flag)

- [ ] **Step 4: Commit**

```bash
git add claw_v2/memory.py claw_v2/observe.py
git commit -m "fix: enable check_same_thread=False on SQLite for asyncio.to_thread safety"
```

---

### Task 5: Telegram Transport

**Files:**
- Create: `claw_v2/telegram.py`
- Create: `tests/test_telegram.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add python-telegram-bot dependency**

In `pyproject.toml`, change:
```toml
dependencies = ["claude-agent-sdk", "openai", "google-genai"]
```
to:
```toml
dependencies = ["claude-agent-sdk", "openai", "google-genai", "python-telegram-bot"]
```

- [ ] **Step 2: Write failing tests**

Create `tests/test_telegram.py`:

```python
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from claw_v2.telegram import TelegramTransport, _split_message


class SplitMessageTests(unittest.TestCase):
    def test_short_message_unchanged(self) -> None:
        self.assertEqual(_split_message("hello"), ["hello"])

    def test_long_message_split(self) -> None:
        text = "a" * 5000
        parts = _split_message(text, max_len=4096)
        self.assertEqual(len(parts), 2)
        self.assertEqual(len(parts[0]), 4096)
        self.assertEqual(len(parts[1]), 904)

    def test_empty_message(self) -> None:
        self.assertEqual(_split_message(""), [""])


class TransportStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_is_noop_without_token(self) -> None:
        transport = TelegramTransport(bot_service=MagicMock(), token=None)
        await transport.start()
        await transport.stop()

    @patch("claw_v2.telegram.ApplicationBuilder")
    async def test_start_builds_and_polls_with_token(self, mock_builder_cls) -> None:
        mock_app = AsyncMock()
        mock_app.updater = AsyncMock()
        mock_builder = MagicMock()
        mock_builder.token.return_value = mock_builder
        mock_builder.build.return_value = mock_app
        mock_builder_cls.return_value = mock_builder

        transport = TelegramTransport(bot_service=MagicMock(), token="test-token")
        await transport.start()
        mock_app.initialize.assert_awaited_once()
        mock_app.start.assert_awaited_once()
        mock_app.updater.start_polling.assert_awaited_once()
        await transport.stop()


class HandleTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized_user_silently_dropped(self) -> None:
        transport = TelegramTransport(
            bot_service=MagicMock(), token="t", allowed_user_id="999",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.message.reply_text = AsyncMock()
        await transport._handle_text(update, MagicMock())
        update.message.reply_text.assert_not_awaited()

    async def test_authorized_user_gets_response(self) -> None:
        bot_service = MagicMock()
        bot_service.handle_text.return_value = "response text"
        transport = TelegramTransport(
            bot_service=bot_service, token="t", allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.text = "hello"
        update.message.reply_text = AsyncMock()

        with patch("claw_v2.telegram.asyncio") as mock_asyncio:
            mock_asyncio.to_thread = AsyncMock(return_value="response text")
            await transport._handle_text(update, MagicMock())

        update.message.reply_text.assert_awaited()


class HandleVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_message_transcribed_and_handled(self) -> None:
        bot_service = MagicMock()
        bot_service.handle_text.return_value = "response to voice"
        transport = TelegramTransport(
            bot_service=bot_service, token="t", allowed_user_id="123",
            voice_api_key="test-key",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.voice.file_id = "file123"
        update.message.voice.file_unique_id = "uniq123"
        update.message.reply_text = AsyncMock()

        mock_file = AsyncMock()
        mock_context = MagicMock()
        mock_context.bot.get_file = AsyncMock(return_value=mock_file)

        with patch("claw_v2.telegram.transcribe", new_callable=AsyncMock, return_value="hola"):
            with patch("claw_v2.telegram.asyncio") as mock_asyncio:
                mock_asyncio.to_thread = AsyncMock(return_value="response to voice")
                await transport._handle_voice(update, mock_context)

        update.message.reply_text.assert_awaited()

    async def test_voice_without_api_key_replies_error(self) -> None:
        from claw_v2.voice import VoiceUnavailableError

        transport = TelegramTransport(
            bot_service=MagicMock(), token="t", allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.message.voice.file_id = "file123"
        update.message.voice.file_unique_id = "uniq123"
        update.message.reply_text = AsyncMock()

        mock_file = AsyncMock()
        mock_context = MagicMock()
        mock_context.bot.get_file = AsyncMock(return_value=mock_file)

        with patch(
            "claw_v2.telegram.transcribe",
            new_callable=AsyncMock,
            side_effect=VoiceUnavailableError("no key"),
        ):
            await transport._handle_voice(update, mock_context)

        update.message.reply_text.assert_awaited_once()
        call_args = update.message.reply_text.call_args[0][0]
        self.assertIn("not available", call_args.lower())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_telegram.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claw_v2.telegram'`

- [ ] **Step 4: Implement telegram.py**

Create `claw_v2/telegram.py`:

```python
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

from claw_v2.bot import BotService

logger = logging.getLogger(__name__)

MAX_TELEGRAM_LEN = 4096


def _split_message(text: str, max_len: int = MAX_TELEGRAM_LEN) -> list[str]:
    if not text:
        return [text]
    parts: list[str] = []
    while text:
        parts.append(text[:max_len])
        text = text[max_len:]
    return parts


class TelegramTransport:
    def __init__(
        self,
        bot_service: BotService,
        token: str | None,
        allowed_user_id: str | None = None,
        voice_api_key: str | None = None,
    ) -> None:
        self._bot_service = bot_service
        self._token = token
        self._allowed_user_id = allowed_user_id
        self._voice_api_key = voice_api_key
        self._app = None

    async def start(self) -> None:
        if self._token is None:
            return
        self._app = ApplicationBuilder().token(self._token).build()
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))
        self._app.add_handler(MessageHandler(filters.COMMAND, self._handle_text))
        self._app.add_handler(MessageHandler(filters.VOICE, self._handle_voice))
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

    async def stop(self) -> None:
        if self._app is None:
            return
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        user_id = str(update.effective_user.id)
        session_id = f"tg-{update.effective_chat.id}"
        text = update.message.text or ""
        try:
            response = await asyncio.to_thread(
                self._bot_service.handle_text, user_id=user_id, session_id=session_id, text=text,
            )
        except Exception:
            logger.exception("Error handling message")
            response = "Error processing your message."
        for part in _split_message(response):
            await update.message.reply_text(part)

    async def _handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        from claw_v2.voice import VoiceUnavailableError, transcribe

        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        tmp_path = Path(f"/tmp/claw-voice-{voice.file_unique_id}.ogg")
        await file.download_to_drive(str(tmp_path))
        try:
            text = await transcribe(tmp_path, api_key=self._voice_api_key)
        except VoiceUnavailableError:
            await update.message.reply_text("Voice not available — OPENAI_API_KEY not configured.")
            return
        finally:
            tmp_path.unlink(missing_ok=True)
        await self._handle_text_content(update, text)

    async def _handle_text_content(self, update: Update, text: str) -> None:
        user_id = str(update.effective_user.id)
        session_id = f"tg-{update.effective_chat.id}"
        try:
            response = await asyncio.to_thread(
                self._bot_service.handle_text, user_id=user_id, session_id=session_id, text=text,
            )
        except Exception:
            logger.exception("Error handling voice message")
            response = "Error processing your voice message."
        for part in _split_message(response):
            await update.message.reply_text(part)

    def _is_authorized(self, update: Update) -> bool:
        if self._allowed_user_id is None:
            return True
        return str(update.effective_user.id) == self._allowed_user_id
```

- [ ] **Step 5: Run telegram tests**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_telegram.py -v`
Expected: all pass

- [ ] **Step 6: Run full test suite**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/ -x -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add claw_v2/telegram.py tests/test_telegram.py pyproject.toml
git commit -m "feat: add Telegram transport layer (python-telegram-bot v20)"
```

---

### Task 6: Lifecycle Module + Main.py Restructure

**Files:**
- Create: `claw_v2/lifecycle.py`
- Create: `tests/test_lifecycle.py`
- Modify: `claw_v2/main.py:175-182`

- [ ] **Step 1: Write failing tests**

Create `tests/test_lifecycle.py`:

```python
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from claw_v2.lifecycle import PidLock, load_soul, run


class PidLockTests(unittest.TestCase):
    def test_acquire_writes_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            lock = PidLock(Path(tmpdir) / "test.pid")
            lock.acquire()
            self.assertTrue(lock.path.exists())
            content = lock.path.read_text().strip()
            self.assertEqual(content, str(os.getpid()))
            lock.release()

    def test_release_removes_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            lock = PidLock(Path(tmpdir) / "test.pid")
            lock.acquire()
            lock.release()
            self.assertFalse(lock.path.exists())

    def test_acquire_fails_if_pid_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "test.pid"
            pid_path.write_text(str(os.getpid()))
            lock = PidLock(pid_path)
            with self.assertRaises(SystemExit):
                lock.acquire()

    def test_acquire_succeeds_if_stale_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "test.pid"
            pid_path.write_text("999999999")
            lock = PidLock(pid_path)
            lock.acquire()
            self.assertEqual(lock.path.read_text().strip(), str(os.getpid()))
            lock.release()


class LoadSoulTests(unittest.TestCase):
    def test_loads_soul_file(self) -> None:
        prompt = load_soul()
        self.assertIn("Claw", prompt)
        self.assertIn("Hector Pachano", prompt)

    def test_fallback_when_no_file(self) -> None:
        prompt = load_soul(Path("/nonexistent/SOUL.md"))
        self.assertEqual(prompt, "You are Claw.")


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_completes_when_daemon_loop_returns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
            }
            with patch.dict(os.environ, env, clear=False):
                with patch("claw_v2.lifecycle.PidLock") as mock_lock_cls:
                    mock_lock = MagicMock()
                    mock_lock_cls.return_value = mock_lock
                    with patch("claw_v2.lifecycle.TelegramTransport") as mock_transport_cls:
                        mock_transport = AsyncMock()
                        mock_transport_cls.return_value = mock_transport
                        with patch("claw_v2.lifecycle.build_runtime") as mock_build:
                            mock_runtime = MagicMock()
                            mock_runtime.config.telegram_bot_token = None
                            mock_runtime.config.telegram_allowed_user_id = None
                            mock_runtime.config.openai_api_key = None
                            mock_runtime.daemon.run_loop = AsyncMock()
                            mock_build.return_value = mock_runtime
                            result = await run()
                            self.assertEqual(result, 0)
                            mock_lock.acquire.assert_called_once()
                            mock_lock.release.assert_called_once()

    async def test_run_skips_telegram_when_no_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
            }
            with patch.dict(os.environ, env, clear=False):
                with patch("claw_v2.lifecycle.PidLock"):
                    with patch("claw_v2.lifecycle.TelegramTransport") as mock_transport_cls:
                        mock_transport = AsyncMock()
                        mock_transport_cls.return_value = mock_transport
                        with patch("claw_v2.lifecycle.build_runtime") as mock_build:
                            mock_runtime = MagicMock()
                            mock_runtime.config.telegram_bot_token = None
                            mock_runtime.config.telegram_allowed_user_id = None
                            mock_runtime.config.openai_api_key = None
                            mock_runtime.daemon.run_loop = AsyncMock()
                            mock_build.return_value = mock_runtime
                            await run()
                            mock_transport_cls.assert_called_once()
                            args = mock_transport_cls.call_args
                            self.assertIsNone(args.kwargs.get("token") or args[1].get("token"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_lifecycle.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claw_v2.lifecycle'`

- [ ] **Step 3: Implement lifecycle.py**

Create `claw_v2/lifecycle.py`:

```python
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PID_PATH = Path.home() / ".claw" / "claw.pid"


def load_soul(soul_path: Path | None = None) -> str:
    if soul_path is None:
        soul_path = Path(__file__).parent / "SOUL.md"
    if soul_path.exists():
        return soul_path.read_text(encoding="utf-8")
    return "You are Claw."


class PidLock:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _DEFAULT_PID_PATH

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                existing_pid = int(self.path.read_text().strip())
                os.kill(existing_pid, 0)
                print(f"Claw is already running (pid {existing_pid}).", file=sys.stderr)
                raise SystemExit(1)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        self.path.write_text(str(os.getpid()))

    def release(self) -> None:
        self.path.unlink(missing_ok=True)


async def run() -> int:
    from claw_v2.main import build_runtime
    from claw_v2.telegram import TelegramTransport

    pid_lock = PidLock()
    pid_lock.acquire()
    try:
        system_prompt = load_soul()
        runtime = build_runtime(system_prompt=system_prompt)
        shutdown = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            loop.add_signal_handler(sig, shutdown.set)

        transport = TelegramTransport(
            bot_service=runtime.bot,
            token=runtime.config.telegram_bot_token,
            allowed_user_id=runtime.config.telegram_allowed_user_id,
            voice_api_key=runtime.config.openai_api_key,
        )

        await transport.start()
        try:
            await runtime.daemon.run_loop(shutdown)
        finally:
            await transport.stop()
    finally:
        pid_lock.release()
    return 0
```

- [ ] **Step 4: Run lifecycle tests**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_lifecycle.py -v`
Expected: 8 passed (4 PidLock + 2 LoadSoul + 2 Run)

- [ ] **Step 5: Update main.py — slim down main()**

In `claw_v2/main.py`, replace lines 175–182:

```python
def main() -> int:
    runtime = build_runtime()
    print(json.dumps(asdict(runtime.heartbeat.collect()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

with:

```python
def main() -> int:
    import asyncio

    from claw_v2.lifecycle import run

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
```

Also clean up imports: remove `import json` (line 2) and `asdict` from the `dataclasses` import (line 4). Neither is used outside the old `main()` function. `ClawRuntime` is a plain dataclass and does not use `asdict`. The `dataclass` import on line 4 is still needed for `@dataclass` on `ClawRuntime`.

- [ ] **Step 6: Run full test suite**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/ -x -q`
Expected: all pass. Note: `test_runtime.py` tests `build_runtime()` directly, not `main()`, so they should still pass.

- [ ] **Step 7: Commit**

```bash
git add claw_v2/lifecycle.py claw_v2/main.py tests/test_lifecycle.py
git commit -m "feat: add lifecycle module (PID lock, signal handlers, async run loop)"
```

---

### Task 7: Launchd Integration

**Files:**
- Create: `ops/claw-launcher.sh`
- Create: `ops/com.pachano.claw.plist`

- [ ] **Step 1: Create ops directory**

Run: `mkdir -p /Users/hector/Projects/Dr.-strange/ops`

- [ ] **Step 2: Create claw-launcher.sh**

Create `ops/claw-launcher.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$HOME/.claw/env" 2>/dev/null || true
cd "$REPO_ROOT"
exec "$REPO_ROOT/.venv/bin/python" -m claw_v2.main
```

- [ ] **Step 3: Make launcher executable**

Run: `chmod +x /Users/hector/Projects/Dr.-strange/ops/claw-launcher.sh`

- [ ] **Step 4: Create com.pachano.claw.plist**

Create `ops/com.pachano.claw.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
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

- [ ] **Step 5: Validate plist syntax**

Run: `plutil -lint /Users/hector/Projects/Dr.-strange/ops/com.pachano.claw.plist`
Expected: `com.pachano.claw.plist: OK`

- [ ] **Step 6: Commit**

```bash
git add ops/claw-launcher.sh ops/com.pachano.claw.plist
git commit -m "feat: add launchd plist and launcher script for daemon mode"
```

---

### Task 8: Final Verification

- [ ] **Step 1: Run full test suite**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/ -v`
Expected: all original 469 tests pass + new tests pass

- [ ] **Step 2: Verify line counts**

Run: `wc -l claw_v2/*.py claw_v2/adapters/*.py | sort -rn | head -20`
Expected: no Python file exceeds 250 lines (except bot.py which is pre-existing tech debt)

- [ ] **Step 3: Verify all new files exist**

Run: `ls -la claw_v2/SOUL.md claw_v2/HEARTBEAT.md claw_v2/CRON.md claw_v2/USER.md claw_v2/SECURITY.md claw_v2/AGENTS.md claw_v2/voice.py claw_v2/telegram.py claw_v2/lifecycle.py ops/claw-launcher.sh ops/com.pachano.claw.plist`
Expected: all 11 files listed

- [ ] **Step 4: Verify import works**

Run: `cd /Users/hector/Projects/Dr.-strange && python -c "from claw_v2.lifecycle import run, PidLock, load_soul; from claw_v2.telegram import TelegramTransport; from claw_v2.voice import transcribe, synthesize; print('all imports OK')"`
Expected: `all imports OK`

- [ ] **Step 5: Commit any remaining fixes**

Only if previous steps required adjustments.
