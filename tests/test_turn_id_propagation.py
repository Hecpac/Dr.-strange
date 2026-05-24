"""P0-B: a single ``turn_id`` correlates every artifact in a Telegram turn.

Behavioral audit found that today the only way to correlate a user
message → dispatch → tool calls → task ledger → approval → response is
to align by wall-clock timestamps, which is brittle. The agent must
generate a stable ``turn_id`` at the entry of every turn and propagate
it through observe events, ``agent_tasks.metadata_json``, approval
metadata, and message artifacts so a single SQL query joins them all.

When a critical operation runs OUTSIDE a turn_id context (e.g. the
daemon's heartbeat tick), the agent must NOT silently drop the
correlator — it must emit a ``turn_id_missing`` structured event so the
gap is visible.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from claw_v2.approval import ApprovalManager
from claw_v2.bot_helpers import (
    current_turn_id,
    new_turn_id,
    turn_id_context,
)
from claw_v2.observe import ObserveStream
from claw_v2.task_ledger import TaskLedger


class TurnIdContextTests(unittest.TestCase):
    def test_current_turn_id_is_none_by_default(self) -> None:
        self.assertIsNone(current_turn_id())

    def test_turn_id_context_sets_and_resets(self) -> None:
        token = new_turn_id()
        with turn_id_context(token):
            self.assertEqual(current_turn_id(), token)
        self.assertIsNone(current_turn_id())

    def test_new_turn_id_is_unique_and_hex_shaped(self) -> None:
        a = new_turn_id()
        b = new_turn_id()
        self.assertNotEqual(a, b)
        # Expect a urlsafe-ish opaque identifier of meaningful length.
        self.assertGreaterEqual(len(a), 8)


class TurnIdPropagationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "claw.db"
        self.observe = ObserveStream(self.db_path)
        self.ledger = TaskLedger(self.db_path, observe=self.observe)
        self.approvals = ApprovalManager(Path(self._tmp.name), secret="secret-x")

    def _read_observe_event_payloads(self, event_type: str) -> list[dict]:
        rows = self.observe._conn.execute(  # type: ignore[attr-defined]
            "SELECT payload FROM observe_stream WHERE event_type=?",
            (event_type,),
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def test_observe_emit_inside_context_attaches_turn_id_to_payload(self) -> None:
        token = new_turn_id()
        with turn_id_context(token):
            self.observe.emit("brain_turn_started", payload={"text_len": 5})
        payloads = self._read_observe_event_payloads("brain_turn_started")
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0].get("turn_id"), token)

    def test_observe_emit_outside_context_emits_turn_id_missing_for_critical_events(self) -> None:
        # Critical brain events emit a turn_id_missing sibling event when
        # they fire outside a turn_id context.
        self.observe.emit("brain_turn_started", payload={"text_len": 5})
        missing = self._read_observe_event_payloads("turn_id_missing")
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].get("origin_event"), "brain_turn_started")

    def test_task_ledger_create_inside_context_persists_turn_id(self) -> None:
        token = new_turn_id()
        with turn_id_context(token):
            self.ledger.create(
                task_id="tg-X",
                session_id="tg-1",
                objective="o",
                runtime="telegram",
                mode="brain_fallback",
                status="running",
            )
        record = self.ledger.get("tg-X")
        assert record is not None
        self.assertEqual(record.metadata.get("turn_id"), token)

    def test_approval_create_inside_context_persists_turn_id(self) -> None:
        token = new_turn_id()
        with turn_id_context(token):
            rec = self.approvals.create("promote_x", "summary", metadata={"k": "v"})
        payload = self.approvals.read(rec.approval_id)
        self.assertEqual(payload["metadata"].get("turn_id"), token)

    def test_behavior_turn_receipt_links_message_tool_task_approval(self) -> None:
        """The full chain — observe event, task ledger row, approval
        record — must all carry the same turn_id when emitted inside the
        same turn_id_context, so a single SQL query joins them."""
        token = new_turn_id()
        with turn_id_context(token):
            self.observe.emit("brain_turn_started", payload={"session_id": "tg-1"})
            self.ledger.create(
                task_id="tg-X",
                session_id="tg-1",
                objective="o",
                runtime="telegram",
                mode="brain_fallback",
                status="running",
            )
            rec = self.approvals.create("promote_x", "summary")

        # observe event
        observe_payloads = self._read_observe_event_payloads("brain_turn_started")
        self.assertEqual(observe_payloads[0].get("turn_id"), token)
        # task ledger metadata
        record = self.ledger.get("tg-X")
        assert record is not None
        self.assertEqual(record.metadata.get("turn_id"), token)
        # approval metadata
        approval_payload = self.approvals.read(rec.approval_id)
        self.assertEqual(approval_payload["metadata"].get("turn_id"), token)
        # And the three turn_id values are equal — the receipt is consistent.
        self.assertEqual(
            {
                observe_payloads[0]["turn_id"],
                record.metadata["turn_id"],
                approval_payload["metadata"]["turn_id"],
            },
            {token},
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
