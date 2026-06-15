"""Tests for the 2026-06-10 audit, group 3 (latency).

1. observe_stream gains a turn_id expression index (turn receipts stop doing
   full-table scans) and a bounded retention prune.
2. Wiki context injection never blocks prompt assembly on wiki.query
   (covered in tests/test_runtime.py::test_brain_injects_wiki_snippets...).
3. Telegram processes updates concurrently with per-chat ordering; operator
   interrupt commands bypass the chat lock.
"""

from __future__ import annotations

import asyncio
import inspect
import tempfile
import unittest
from pathlib import Path

from claw_v2.observe import ObserveStream
from claw_v2.telegram import TelegramTransport, _is_interrupt_command


class ObserveStreamTurnIdIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.observe = ObserveStream(Path(tempfile.mkdtemp()) / "observe.db")

    def test_turn_receipt_lookup_uses_expression_index(self) -> None:
        self.observe.emit("tool_call", payload={"turn_id": "turn-1"})
        plan = self.observe._conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT event_type, payload FROM observe_stream
            WHERE json_extract(payload, '$.turn_id') = ?
            """,
            ("turn-1",),
        ).fetchall()
        plan_text = " ".join(str(row) for row in plan)
        self.assertIn("idx_observe_stream_turn_id", plan_text)

    def test_prune_deletes_only_rows_older_than_retention(self) -> None:
        self.observe.emit("recent_event", payload={"x": 1})
        with self.observe._lock:
            self.observe._conn.execute(
                """
                INSERT INTO observe_stream (event_type, payload, timestamp)
                VALUES ('old_event', '{}', datetime('now', '-90 days'))
                """
            )
            self.observe._conn.commit()

        deleted = self.observe.prune(retention_days=30)

        self.assertEqual(deleted, 1)
        remaining = [
            row[0]
            for row in self.observe._conn.execute(
                "SELECT event_type FROM observe_stream"
            ).fetchall()
        ]
        self.assertIn("recent_event", remaining)
        self.assertNotIn("old_event", remaining)

    def test_prune_is_bounded_per_call(self) -> None:
        with self.observe._lock:
            for _ in range(5):
                self.observe._conn.execute(
                    """
                    INSERT INTO observe_stream (event_type, payload, timestamp)
                    VALUES ('old_event', '{}', datetime('now', '-90 days'))
                    """
                )
            self.observe._conn.commit()

        self.assertEqual(self.observe.prune(retention_days=30, max_rows=2), 2)
        self.assertEqual(self.observe.prune(retention_days=30, max_rows=10), 3)

    def test_prune_caps_total_row_count(self) -> None:
        # 50 fresh rows (all within retention): age-only prune keeps them all,
        # but max_total_rows must cap the table to its highest 20 ids.
        with self.observe._lock:
            for _ in range(50):
                self.observe._conn.execute(
                    "INSERT INTO observe_stream (event_type, payload) VALUES ('e', '{}')"
                )
            self.observe._conn.commit()

        deleted = self.observe.prune(retention_days=30, max_rows=1000, max_total_rows=20)

        self.assertEqual(deleted, 30)
        rows = self.observe._conn.execute("SELECT id FROM observe_stream ORDER BY id").fetchall()
        self.assertEqual(len(rows), 20)
        ids = [r[0] for r in rows]
        # survivors are the highest ids (31..50)
        self.assertEqual(ids, list(range(31, 51)))


class ObservePruneSchedulerJobTests(unittest.TestCase):
    def test_observe_prune_job_is_registered_and_runs(self) -> None:
        import os
        from unittest.mock import patch

        from claw_v2.adapters.base import LLMRequest
        from claw_v2.main import build_runtime
        from claw_v2.types import LLMResponse

        def fake_anthropic(request: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="<response>ok</response>", lane=request.lane, provider="anthropic"
            )

        root = Path(tempfile.mkdtemp())
        env = {
            "DB_PATH": str(root / "data" / "claw.db"),
            "WORKSPACE_ROOT": str(root / "workspace"),
            "AGENT_STATE_ROOT": str(root / "agents"),
            "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
            "APPROVALS_ROOT": str(root / "approvals"),
            "PIPELINE_STATE_ROOT": str(root / "pipeline"),
        }
        with patch.dict(os.environ, env, clear=False):
            runtime = build_runtime(anthropic_executor=fake_anthropic)
            jobs = {job.name: job for job in runtime.scheduler.list_jobs()}
            self.assertIn("observe_prune", jobs)
            with runtime.observe._lock:
                runtime.observe._conn.execute(
                    """
                    INSERT INTO observe_stream (event_type, payload, timestamp)
                    VALUES ('old_event', '{}', datetime('now', '-90 days'))
                    """
                )
                runtime.observe._conn.commit()
            # Must not raise (a NameError here would be swallowed by the
            # daemon's tick wrapper in production) and must prune + audit.
            jobs["observe_prune"].handler()
            remaining = runtime.observe._conn.execute(
                "SELECT COUNT(*) FROM observe_stream WHERE event_type='old_event'"
            ).fetchone()[0]
            self.assertEqual(remaining, 0)
            recent = [e["event_type"] for e in runtime.observe.recent_events(limit=10)]
            self.assertIn("observe_stream_pruned", recent)


class InterruptCommandMatcherTests(unittest.TestCase):
    def test_operator_interrupts_match(self) -> None:
        for text in (
            "/freeze",
            "/unfreeze",
            "/status",
            "/approvals",
            "/approve abc token",
            "/action_abort abc",
            "/FREEZE",
            "/freeze@DrStrangeBot",
        ):
            with self.subTest(text=text):
                self.assertTrue(_is_interrupt_command(text))

    def test_regular_messages_and_other_commands_do_not_match(self) -> None:
        for text in (
            "hola",
            "/computer abre chrome",
            "/design un mockup",
            "/task_run",
            "aprueba el deploy",
            "",
        ):
            with self.subTest(text=text):
                self.assertFalse(_is_interrupt_command(text))


class _BlockingBotService:
    """Fake BotService: first call blocks until released; records ordering."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.release = asyncio.Event()
        self.first_started = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def handle_text(
        self,
        *,
        user_id: str,
        session_id: str,
        text: str,
        runtime_channel: str,
        context_metadata=None,
    ) -> str:
        loop = self._loop
        assert loop is not None
        self.calls.append(f"start:{text}")
        if text == "turno-largo":
            asyncio.run_coroutine_threadsafe(self._wait_release(), loop).result(timeout=5)
        self.calls.append(f"end:{text}")
        return f"ok:{text}"

    async def _wait_release(self) -> None:
        self.first_started.set()
        await self.release.wait()


class TelegramChatLockTests(unittest.IsolatedAsyncioTestCase):
    def _transport(self, bot: _BlockingBotService) -> TelegramTransport:
        transport = TelegramTransport(bot_service=bot, token=None)
        return transport

    async def test_same_chat_turns_are_serialized(self) -> None:
        bot = _BlockingBotService()
        bot._loop = asyncio.get_running_loop()
        transport = self._transport(bot)

        long_turn = asyncio.create_task(
            transport._handle_agent_text(user_id="1", session_id="tg-1", text="turno-largo")
        )
        await bot.first_started.wait()
        second_turn = asyncio.create_task(
            transport._handle_agent_text(user_id="1", session_id="tg-1", text="segundo")
        )
        await asyncio.sleep(0.1)
        # The second turn must not have started while the first holds the lock.
        self.assertNotIn("start:segundo", bot.calls)

        bot.release.set()
        self.assertEqual(await long_turn, "ok:turno-largo")
        self.assertEqual(await second_turn, "ok:segundo")
        self.assertEqual(
            bot.calls,
            ["start:turno-largo", "end:turno-largo", "start:segundo", "end:segundo"],
        )

    async def test_interrupt_command_bypasses_chat_lock(self) -> None:
        bot = _BlockingBotService()
        bot._loop = asyncio.get_running_loop()
        transport = self._transport(bot)

        long_turn = asyncio.create_task(
            transport._handle_agent_text(user_id="1", session_id="tg-1", text="turno-largo")
        )
        await bot.first_started.wait()
        # /freeze must complete while the long turn still holds the chat lock.
        result = await asyncio.wait_for(
            transport._handle_agent_text(user_id="1", session_id="tg-1", text="/freeze"),
            timeout=2.0,
        )
        self.assertEqual(result, "ok:/freeze")
        self.assertIn("end:/freeze", bot.calls)
        self.assertNotIn("end:turno-largo", bot.calls)

        bot.release.set()
        self.assertEqual(await long_turn, "ok:turno-largo")

    def test_video_multimodal_turn_runs_under_chat_lock(self) -> None:
        # AH7/M19 (2026-06-11): video turns must hold the same per-chat lock
        # as text (2052), image (1813) and document (1878) turns; with
        # concurrent_updates a video+text pair in the same chat otherwise
        # races the session-state read-modify-write (lost update).
        source = inspect.getsource(TelegramTransport._handle_video)
        lock_idx = source.find("async with self._chat_lock(session_id):")
        call_idx = source.find("self._handle_agent_multimodal_sync")
        self.assertNotEqual(lock_idx, -1, "video handler must take the chat lock")
        self.assertNotEqual(call_idx, -1)
        self.assertLess(lock_idx, call_idx, "the lock must wrap the multimodal turn")


if __name__ == "__main__":
    unittest.main()
