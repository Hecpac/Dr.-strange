"""Receipts must summarize the turn's behavior in one structured event.

The receipt design comes from the 2026-05-23 behavioral audit
recommendation R7 ("Behavior receipt por turno con intent, tools,
approval, evidence, verification") and is a prerequisite for shipping
auditable workflows / semantic approvals.

Tests cover the pure builder and the full emit-via-ObserveStream path.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from claw_v2.observe import ObserveStream
from claw_v2.turn_context import new_turn_id, turn_id_context
from claw_v2.turn_receipt import (
    aggregate_observe_events,
    build_turn_receipt_payload,
    emit_turn_receipt,
    hash_user_text,
)


class _Row(dict):
    """sqlite3.Row-ish: supports both ``row["event_type"]`` and ``.keys()``."""


def _evt(event_type: str, **payload):
    return _Row(event_type=event_type, payload=json.dumps(payload))


class HashUserTextTests(unittest.TestCase):
    def test_hash_is_deterministic_and_short(self) -> None:
        h1 = hash_user_text("dale dispara")
        h2 = hash_user_text("dale dispara")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 16)
        self.assertNotEqual(h1, hash_user_text("dale otra cosa"))


class AggregateObserveEventsTests(unittest.TestCase):
    def test_buckets_dispatch_tools_approvals_and_task_ids(self) -> None:
        rows = [
            _evt("semantic_turn_trace", semantic_intent="audit_request"),
            _evt("dispatch_decision", handler="brain_first_semantic", captured=True),
            _evt("dispatch_decision", handler="capability_route", captured=False),
            _evt("sdk_post_tool_use", tool_name="Read"),
            _evt("sdk_post_tool_use", tool_name="Bash"),
            _evt("sdk_post_tool_use_failure", tool_name="Write"),
            _evt("approval_pending", action="promote_self-improve"),
            _evt("task_ledger_created", task_id="brain-tooluse:tg-x:1"),
            _evt("llm_response", cost_estimate=0.0125),
        ]
        agg = aggregate_observe_events(rows)
        self.assertEqual(agg["intents"], ["audit_request"])
        self.assertEqual(agg["handlers_matched"], ["brain_first_semantic"])
        self.assertEqual(sorted(agg["tools"]), ["Bash", "Read"])
        self.assertEqual(agg["tool_failures"], ["Write"])
        self.assertEqual(agg["approvals_requested"], ["promote_self-improve"])
        self.assertEqual(agg["task_ids"], ["brain-tooluse:tg-x:1"])
        self.assertAlmostEqual(agg["cost_estimate"], 0.0125, places=6)


class BuildTurnReceiptPayloadTests(unittest.TestCase):
    def test_payload_has_required_fields(self) -> None:
        rows = [
            _evt("dispatch_decision", handler="brain_first_semantic", captured=True),
            _evt("sdk_post_tool_use", tool_name="Read"),
        ]
        payload = build_turn_receipt_payload(
            turn_id="t1",
            session_id="tg-1",
            user_text="dale",
            started_at=1000.0,
            completed_at=1001.5,
            observe_rows=rows,
        )
        for key in (
            "turn_id",
            "session_id",
            "user_text_hash",
            "user_text_length",
            "started_at",
            "completed_at",
            "duration_ms",
            "intent",
            "handlers_matched",
            "tools_used",
            "tools_invoked_count",
            "tool_failures",
            "approvals_requested",
            "ledger_task_ids",
            "cost_estimate",
        ):
            self.assertIn(key, payload, f"missing key {key}")
        self.assertEqual(payload["turn_id"], "t1")
        self.assertEqual(payload["session_id"], "tg-1")
        self.assertEqual(payload["duration_ms"], 1500)
        self.assertEqual(payload["tools_used"], ["Read"])
        self.assertEqual(payload["tools_invoked_count"], 1)
        self.assertEqual(payload["user_text_length"], 4)

    def test_payload_does_not_include_raw_user_text(self) -> None:
        payload = build_turn_receipt_payload(
            turn_id="t1",
            session_id="tg-1",
            user_text="dale dispara contraseña 12345 secret",
            started_at=0.0,
            completed_at=1.0,
            observe_rows=[],
        )
        serialized = json.dumps(payload)
        self.assertNotIn("dispara", serialized)
        self.assertNotIn("contraseña", serialized)
        self.assertNotIn("12345", serialized)
        self.assertNotIn("secret", serialized)

    def test_payload_includes_ledger_status_when_record_given(self) -> None:
        record = SimpleNamespace(
            status="completed_unverified",
            verification_status="needs_verification",
            artifacts={"evidence_manifest": {"tools_run": ["Read"]}},
        )
        payload = build_turn_receipt_payload(
            turn_id="t1",
            session_id="tg-1",
            user_text="dale",
            started_at=0.0,
            completed_at=1.0,
            observe_rows=[],
            ledger_record=record,
            learning_outcome="usable_reply_unverified",
        )
        self.assertEqual(payload["ledger_status"], "completed_unverified")
        self.assertEqual(payload["verification_status"], "needs_verification")
        self.assertTrue(payload["evidence_manifest_present"])
        self.assertEqual(payload["learning_outcome"], "usable_reply_unverified")


class EmitTurnReceiptTests(unittest.TestCase):
    def test_emit_reads_observe_rows_by_turn_id_and_emits_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            observe = ObserveStream(Path(tmp) / "claw.db")
            turn_id = new_turn_id()
            with turn_id_context(turn_id):
                observe.emit("dispatch_decision", payload={"handler": "h", "captured": True})
                observe.emit("sdk_post_tool_use", payload={"tool_name": "Read"})
                observe.emit("sdk_post_tool_use", payload={"tool_name": "Bash"})
                payload = emit_turn_receipt(
                    observe,
                    turn_id=turn_id,
                    session_id="tg-1",
                    user_text="dale",
                    started_at=1000.0,
                    completed_at=1002.0,
                )
            # the emit_turn_receipt call returns the payload it sent
            self.assertEqual(payload["turn_id"], turn_id)
            self.assertEqual(payload["session_id"], "tg-1")
            self.assertEqual(sorted(payload["tools_used"]), ["Bash", "Read"])
            self.assertEqual(payload["duration_ms"], 2000)
            # And the receipt itself is persisted in observe_stream with its turn_id.
            rows = observe._conn.execute(
                """
                SELECT payload FROM observe_stream
                WHERE event_type='turn_receipt'
                  AND json_extract(payload, '$.turn_id') = ?
                """,
                (turn_id,),
            ).fetchall()
            self.assertEqual(len(rows), 1)
            persisted = json.loads(rows[0][0])
            self.assertEqual(persisted["turn_id"], turn_id)
            self.assertEqual(persisted["tools_invoked_count"], 2)

    def test_emit_includes_only_rows_with_matching_turn_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            observe = ObserveStream(Path(tmp) / "claw.db")
            turn_a = new_turn_id()
            turn_b = new_turn_id()
            with turn_id_context(turn_a):
                observe.emit("sdk_post_tool_use", payload={"tool_name": "Read"})
            with turn_id_context(turn_b):
                observe.emit("sdk_post_tool_use", payload={"tool_name": "Bash"})
                receipt = emit_turn_receipt(
                    observe,
                    turn_id=turn_b,
                    session_id="tg-2",
                    user_text="x",
                    started_at=0.0,
                    completed_at=1.0,
                )
            # Receipt for turn_b must include only Bash, not Read.
            self.assertEqual(receipt["tools_used"], ["Bash"])

    def test_emit_swallows_query_errors_to_avoid_breaking_turn(self) -> None:
        # Faux observe with no _conn — emit_turn_receipt must not crash.
        class _Stub:
            def __init__(self) -> None:
                self.events: list[tuple[str, dict]] = []

            def emit(self, event_type: str, *, payload: dict | None = None) -> None:
                self.events.append((event_type, payload or {}))

        observe = _Stub()
        payload = emit_turn_receipt(
            observe,
            turn_id="t1",
            session_id="tg-1",
            user_text="x",
            started_at=0.0,
            completed_at=1.0,
        )
        # Receipt still built and emitted, just with empty buckets.
        self.assertEqual(payload["turn_id"], "t1")
        self.assertEqual(payload["tools_used"], [])
        self.assertEqual(observe.events[0][0], "turn_receipt")


class HandleTextEmitsTurnReceiptTests(unittest.TestCase):
    def test_handle_text_emits_turn_receipt_event_on_finish(self) -> None:
        """End-to-end: a stubbed handle_text body returns; the finally
        block in the handle_text wrapper must emit a ``turn_receipt``
        event carrying the active turn_id."""
        from types import MethodType
        from claw_v2.bot import BotService
        from claw_v2.observe import ObserveStream

        with tempfile.TemporaryDirectory() as tmp:
            observe = ObserveStream(Path(tmp) / "claw.db")

            bot = BotService.__new__(BotService)
            bot.allowed_user_id = "user-1"
            bot.observe = observe

            def _body(self, *, user_id, session_id, text, runtime_channel, context_metadata):
                # Mimic a handler doing some work: emit a couple of
                # observe events tagged via the active turn_id_context.
                observe.emit("dispatch_decision", payload={"handler": "h", "captured": True})
                observe.emit("sdk_post_tool_use", payload={"tool_name": "Read"})
                return "ok"

            bot._handle_text_body = MethodType(_body, bot)  # type: ignore[method-assign]

            result = bot.handle_text(user_id="user-1", session_id="tg-1", text="hola")
            self.assertEqual(result, "ok")

            rows = observe._conn.execute(
                "SELECT payload FROM observe_stream WHERE event_type='turn_receipt'"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            payload = json.loads(rows[0][0])
            self.assertEqual(payload["session_id"], "tg-1")
            self.assertEqual(payload["tools_used"], ["Read"])
            self.assertEqual(payload["handlers_matched"], ["h"])
            self.assertIn("turn_id", payload)
            self.assertEqual(payload["user_text_length"], 4)

    def test_handle_text_emits_turn_receipt_even_on_exception(self) -> None:
        """If _handle_text_body raises, the finally block must still
        emit a turn_receipt — the receipt is the post-mortem hook."""
        from types import MethodType
        from claw_v2.bot import BotService
        from claw_v2.observe import ObserveStream

        with tempfile.TemporaryDirectory() as tmp:
            observe = ObserveStream(Path(tmp) / "claw.db")

            bot = BotService.__new__(BotService)
            bot.allowed_user_id = "user-1"
            bot.observe = observe

            class _Boom(RuntimeError):
                pass

            def _body(self, **_):
                raise _Boom("body crashed")

            bot._handle_text_body = MethodType(_body, bot)  # type: ignore[method-assign]

            with self.assertRaises(_Boom):
                bot.handle_text(user_id="user-1", session_id="tg-1", text="x")

            rows = observe._conn.execute(
                "SELECT 1 FROM observe_stream WHERE event_type='turn_receipt'"
            ).fetchall()
            self.assertEqual(len(rows), 1, "receipt must survive exception in body")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
