"""P2 follow-up: ``BotService.handle_text`` must open a ``turn_id_context``
before any downstream call.

In the P0/P1 branch we wired ``turn_id`` propagation through observe,
task_ledger, and approval — but ``handle_text`` itself did NOT open the
context. So in production every observe event, ledger row, and approval
created during a Telegram turn lacked the correlator, defeating the
behavior-receipt design. ``task_ledger_created`` was also temporarily
removed from ``CRITICAL_OBSERVE_EVENTS_REQUIRING_TURN_ID`` to avoid a
``turn_id_missing`` flood. This PR re-enables both.
"""

from __future__ import annotations

import inspect
import tempfile
import types
import unittest
from pathlib import Path

from claw_v2.bot import BotService
from claw_v2.observe import ObserveStream
from claw_v2.turn_context import (
    CRITICAL_OBSERVE_EVENTS_REQUIRING_TURN_ID,
    current_turn_id,
)


class HandleTextOpensTurnIdContextTests(unittest.TestCase):
    def test_handle_text_source_contains_turn_id_context_open(self) -> None:
        """Static guard: the source of handle_text must reference
        ``turn_id_context`` and ``new_turn_id`` so the wiring is not
        accidentally dropped by a future refactor."""
        src = inspect.getsource(BotService.handle_text)
        self.assertIn("turn_id_context", src, "handle_text must open turn_id_context")
        self.assertIn("new_turn_id", src, "handle_text must generate a new_turn_id")

    def test_handle_text_sets_current_turn_id_for_downstream_calls(self) -> None:
        """Patch the first downstream call (`_ensure_default_autonomy`) so
        it captures ``current_turn_id()`` and raises to short-circuit.
        Pre-fix: captured value is None. Post-fix: a non-None token."""
        captured: dict[str, object] = {}

        class _ShortCircuit(RuntimeError):
            pass

        def _capture(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            captured["turn_id_inside_handle_text"] = current_turn_id()
            raise _ShortCircuit("captured")

        bot = BotService.__new__(BotService)
        bot.allowed_user_id = "user-1"
        bot._ensure_default_autonomy = types.MethodType(_capture, bot)  # type: ignore[method-assign]

        with self.assertRaises(_ShortCircuit):
            bot.handle_text(user_id="user-1", session_id="tg-x", text="hola")

        self.assertIsNotNone(
            captured.get("turn_id_inside_handle_text"),
            "handle_text must open a turn_id_context before any other call",
        )
        # And after handle_text returns, the context must be reset.
        self.assertIsNone(current_turn_id(), "context must be reset on __exit__")

    def test_task_ledger_created_re_enabled_in_critical_set(self) -> None:
        """Now that handle_text opens turn_id_context, the
        task_ledger_created event must be back in the CRITICAL set so a
        rogue daemon-side create (no turn_id) still emits a
        ``turn_id_missing`` sibling."""
        self.assertIn(
            "task_ledger_created",
            CRITICAL_OBSERVE_EVENTS_REQUIRING_TURN_ID,
            "task_ledger_created must be reactivated once handle_text wires turn_id",
        )

    def test_observe_event_emitted_inside_handle_text_carries_turn_id(self) -> None:
        """End-to-end: an observe event emitted by a stubbed handler
        inside handle_text picks up the active turn_id via the
        ObserveStream auto-inject path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "claw.db"
            observe = ObserveStream(db)

            captured_event_payloads: list[dict] = []

            class _StopHere(RuntimeError):
                pass

            def _capture_emit_then_stop(self, *_a, **_kw):  # type: ignore[no-untyped-def]
                # Emit a known event that lives in the CRITICAL set so we
                # can assert downstream behavior. ``brain_turn_started``
                # is already CRITICAL.
                observe.emit("brain_turn_started", payload={"text_len": 5})
                rows = observe._conn.execute(  # type: ignore[attr-defined]
                    "SELECT payload FROM observe_stream WHERE event_type='brain_turn_started' ORDER BY id DESC LIMIT 1"
                ).fetchall()
                import json
                captured_event_payloads.extend(json.loads(r[0]) for r in rows)
                raise _StopHere("done")

            bot = BotService.__new__(BotService)
            bot.allowed_user_id = "user-1"
            bot._ensure_default_autonomy = types.MethodType(_capture_emit_then_stop, bot)  # type: ignore[method-assign]

            with self.assertRaises(_StopHere):
                bot.handle_text(user_id="user-1", session_id="tg-x", text="hi")

        self.assertEqual(len(captured_event_payloads), 1)
        self.assertIn(
            "turn_id",
            captured_event_payloads[0],
            "events emitted inside handle_text must carry the active turn_id",
        )
        self.assertTrue(
            captured_event_payloads[0]["turn_id"],
            "turn_id must be a non-empty token",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
