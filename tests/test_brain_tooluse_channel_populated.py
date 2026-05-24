"""P0-D: brain-fallback tasks must populate `channel` on the ledger row.

Behavioral audit found 99/100 `brain-tooluse:tg-*` rows in `agent_tasks`
with `channel=NULL`, even though the `task_id` clearly originates from
Telegram. Root cause: `_attach_brain_tool_use_ledger` calls
`TaskLedger.create(...)` without a `route=` argument, and `channel` is
derived from `route.get("channel")` (see `task_ledger.py:175`).

Fix: pass `route={"channel": ..., "external_session_id": session_id, ...}`
so the ledger persists a non-NULL channel for downstream audits and
behavior receipts.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.test_brain_tooluse_ledger import (
    _RecordingObserve,
    _StubResponse,
    _make_bot,
    _tool_event,
)

from claw_v2.task_ledger import TaskLedger


class BrainToolUseChannelPopulatedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = TaskLedger(Path(self._tmp.name) / "claw.db")

    def test_brain_tooluse_telegram_channel_is_persisted(self) -> None:
        """When runtime_channel='telegram' is given, the ledger row must
        store channel='telegram' (not NULL) and external_session_id."""
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Read", tool_input={"file_path": "/etc/hosts"}),
        ]
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-574707975",
            response=_StubResponse(artifacts={"trace_id": "trace-A"}),
            source_text="audita",
            runtime_channel="telegram",
        )
        recent = self.ledger.list(limit=10)
        self.assertEqual(len(recent), 1, "brain tool-use ledger row missing")
        task = recent[0]
        self.assertEqual(task.channel, "telegram")
        self.assertEqual(task.external_session_id, "tg-574707975")
        self.assertEqual(task.route.get("channel"), "telegram")

    def test_brain_tooluse_infers_telegram_from_session_prefix(self) -> None:
        """If runtime_channel is missing but session_id starts with 'tg-',
        the channel must still be marked 'telegram' (Telegram is the
        canonical surface for tg-* sessions)."""
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Read", tool_input={"file_path": "/etc/hosts"}),
        ]
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-574707975",
            response=_StubResponse(artifacts={"trace_id": "trace-B"}),
            source_text="dale dispara",
            runtime_channel=None,
        )
        recent = self.ledger.list(limit=10)
        self.assertEqual(len(recent), 1)
        task = recent[0]
        self.assertEqual(task.channel, "telegram")
        self.assertEqual(task.external_session_id, "tg-574707975")

    def test_brain_tooluse_non_telegram_session_does_not_force_telegram(self) -> None:
        """Web-chat / mac-main sessions must NOT be mislabeled as telegram."""
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Read", tool_input={"file_path": "/etc/hosts"}),
        ]
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="mac-main",
            response=_StubResponse(artifacts={"trace_id": "trace-C"}),
            source_text="local probe",
            runtime_channel=None,
        )
        recent = self.ledger.list(limit=10)
        self.assertEqual(len(recent), 1)
        task = recent[0]
        self.assertNotEqual(task.channel, "telegram")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
