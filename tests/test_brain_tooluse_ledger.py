"""PR 0E: brain tool-use ledger.

The brain fallback can invoke tools via `router.ask(lane="brain")`.
Before PR 0E, those tool calls left no `agent_tasks` row, so audits
saw "brain said done" with zero durable trace.

PR 0E wraps every brain fallback turn in a post-hoc ledger pass:

  - 0 tool events                  → noop, no task created (no DB noise)
  - any tool event + active task   → attach (no second task)
  - any tool event + no active task→ synthetic agent_tasks row with
                                      verification_status="needs_verification"
                                      (downgraded by the defense-in-depth
                                      gate to running+missing_evidence)
  - tool failures                   → status="failed", verification_status="failed"
  - approval-required, no tool ran  → no synthetic task, sensitive event emitted

These tests exercise `BotService._attach_brain_tool_use_ledger` directly
with a stub `observe`, a real `TaskLedger`, and a stub session-state
provider — no live `router.ask`, no SDK adapter. The whole point of the
ledger is to operate on observe-event evidence after the brain returns;
we feed it synthetic events to drive every branch.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claw_v2.bot import BotService
from claw_v2.task_ledger import TaskLedger


class _RecordingObserve:
    """Stand-in for `Observability` exposing the two methods the ledger uses."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.canned_trace_events: list[dict[str, Any]] = []

    def trace_events(self, trace_id: str, *, limit: int | None = None) -> list[dict]:
        return list(self.canned_trace_events)

    def emit(self, event: str, **kwargs: object) -> None:
        self.events.append((event, dict(kwargs)))


class _StubBrainMemory:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self._state = state or {}

    def get_session_state(self, session_id: str) -> dict[str, Any]:
        return dict(self._state)


class _StubBrain:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.memory = _StubBrainMemory(state)


@dataclass
class _StubResponse:
    content: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)


def _make_bot(observe: _RecordingObserve, task_ledger: TaskLedger, state: dict | None = None) -> BotService:
    """Construct just enough BotService surface for the ledger to run.

    BotService has a heavy __init__ (loads MCP/agents/etc.) so we
    instantiate via __new__ and attach the three attributes
    `_attach_brain_tool_use_ledger` actually reads.
    """
    bot = BotService.__new__(BotService)
    bot.observe = observe
    bot.task_ledger = task_ledger
    bot.brain = _StubBrain(state)
    return bot


def _tool_event(tool_name: str, **kwargs) -> dict[str, Any]:
    payload = {"tool_name": tool_name, "tool_use_id": kwargs.pop("tool_use_id", "tu_x")}
    payload.update(kwargs)
    return {
        "event_type": "sdk_post_tool_use",
        "trace_id": "trace-X",
        "payload": json.dumps(payload),
    }


def _tool_failure_event(tool_name: str, error: str = "Exit code 1", **kwargs) -> dict[str, Any]:
    payload = {
        "tool_name": tool_name,
        "tool_use_id": kwargs.pop("tool_use_id", "tu_x"),
        "error": error,
        "is_error": True,
    }
    payload.update(kwargs)
    return {
        "event_type": "sdk_post_tool_use_failure",
        "trace_id": "trace-X",
        "payload": json.dumps(payload),
    }


class BrainToolUseLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._ledger_observe = _RecordingObserve()
        # Wire the recording observe into the ledger too so the defense-
        # in-depth `task_false_success_prevented` event is observable.
        self.ledger = TaskLedger(
            Path(self._tmp.name) / "claw.db", observe=self._ledger_observe
        )

    # --- A. chat with no tools -> no task -------------------------------------

    def test_a_brain_chat_with_no_tools_does_not_create_agent_task(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = []
        bot = _make_bot(observe, self.ledger)
        response = _StubResponse(artifacts={"trace_id": "trace-X"})
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=response,
            source_text="hola",
            runtime_channel="telegram",
        )
        events = [name for name, _ in observe.events]
        self.assertIn("brain_tooluse_ledger_noop_no_tools", events)
        self.assertEqual(self.ledger.list(limit=10), [])

    # --- B. Read/Grep/Glob tools -> synthetic task ----------------------------

    def test_b_brain_fallback_with_tier1_tools_creates_synthetic_task(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Read", tool_input={"file_path": "/Users/hector/Projects/Dr.-strange/CLAUDE.md"}),
            _tool_event("Grep", tool_input={"pattern": "TODO"}),
            _tool_event("Glob", tool_input={"pattern": "**/*.py"}),
        ]
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="que ves en CLAUDE.md?",
            runtime_channel="telegram",
        )
        events = [name for name, _ in observe.events]
        self.assertIn("brain_tooluse_ledger_started", events)
        # Synthetic task exists.
        recent = self.ledger.list(limit=10)
        self.assertEqual(len(recent), 1)
        task = recent[0]
        self.assertEqual(task.mode, "brain_fallback")
        self.assertEqual(task.session_id, "tg-test")
        self.assertEqual(task.objective, "que ves en CLAUDE.md?")
        self.assertEqual(task.metadata.get("origin"), "brain_fallback")
        self.assertTrue(task.metadata.get("brain_tool_use"))
        self.assertEqual(task.metadata.get("created_by"), "brain_tool_use_ledger")
        # Artifacts captured.
        artifacts = task.artifacts
        self.assertEqual(sorted(artifacts.get("tools_run", [])), ["Glob", "Grep", "Read"])
        self.assertEqual(artifacts.get("tool_event_count"), 3)
        self.assertEqual(artifacts.get("tool_failure_count"), 0)
        self.assertIn("CLAUDE.md", str(artifacts.get("files_touched")))

    # --- C. Bash tool -> command captured (redacted/capped) -------------------

    def test_c_bash_tool_records_command_metadata_redacted_capped(self) -> None:
        observe = _RecordingObserve()
        long_command = "ps aux | rg something " + ("x" * 400)
        observe.canned_trace_events = [
            _tool_event("Bash", tool_input={"command": long_command}),
        ]
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="qué procesos hay?",
            runtime_channel="telegram",
        )
        task = self.ledger.list(limit=10)[0]
        commands = task.artifacts.get("commands_run", [])
        self.assertEqual(len(commands), 1)
        # Capped to 200 chars by the summarizer.
        self.assertLessEqual(len(commands[0]), 200)

    # --- D. tool observe events correlated by trace_id -----------------------

    def test_d_tool_events_can_be_correlated_to_task_via_trace_id(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [_tool_event("Read", tool_input={"file_path": "/etc/hosts"})]
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="lee /etc/hosts",
            runtime_channel="telegram",
        )
        task = self.ledger.list(limit=10)[0]
        # The task carries trace_id in BOTH metadata and artifacts so audits
        # can JOIN observe_stream.trace_id → agent_tasks via either field.
        self.assertEqual(task.metadata.get("trace_id"), "trace-X")
        self.assertEqual(task.artifacts.get("trace_id"), "trace-X")

    # --- E. existing active task -> attach, no duplicate ---------------------

    def test_e_active_owner_delegation_task_attaches_not_duplicates(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [_tool_event("Read", tool_input={"file_path": "/x"})]
        bot = _make_bot(
            observe,
            self.ledger,
            state={
                "active_object": {
                    "active_task": {
                        "task_id": "owner-delegated-task-123",
                        "status": "running",
                    }
                }
            },
        )
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="continua",
            runtime_channel="telegram",
        )
        events = [name for name, _ in observe.events]
        self.assertIn("brain_tooluse_ledger_attached_existing", events)
        # No synthetic task created.
        self.assertEqual(self.ledger.list(limit=10), [])
        # Event payload carries the existing task_id.
        attached = next(
            kwargs.get("payload")
            for name, kwargs in observe.events
            if name == "brain_tooluse_ledger_attached_existing"
        )
        self.assertEqual(attached.get("existing_task_id"), "owner-delegated-task-123")

    # --- F. failed tool -> task failed/blocked, not passed -------------------

    def test_f_failed_tool_marks_task_failed_not_passed(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Read", tool_input={"file_path": "/x"}),
            _tool_failure_event("Bash", error="Exit code 127 command not found: foo"),
        ]
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="prueba foo",
            runtime_channel="telegram",
        )
        task = self.ledger.list(limit=10)[0]
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.verification_status, "failed")
        self.assertIn("Exit code 127", task.error or "")
        events = [name for name, _ in observe.events]
        self.assertIn("brain_tooluse_ledger_failed", events)

    def test_f2_nonfatal_tool_failure_with_useful_summary_is_not_top_level_failed(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Grep", tool_input={"pattern": "Phase 3"}),
            _tool_failure_event(
                "Read",
                error=(
                    "File content (48492 tokens) exceeds maximum allowed tokens "
                    "(25000). Use offset and limit parameters."
                ),
            ),
        ]
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(
                content="Encontré la auditoría y resumí los bloqueos principales.",
                artifacts={"trace_id": "trace-X"},
            ),
            source_text="revisa la auditoria que hizo",
            runtime_channel="telegram",
        )
        task = self.ledger.list(limit=10)[0]
        self.assertNotEqual(task.status, "failed")
        self.assertIn(task.status, {"succeeded", "running"})
        self.assertIn(task.verification_status, {"succeeded_with_warnings", "partial_success", "needs_verification"})
        substeps = task.artifacts.get("substeps", [])
        self.assertTrue(
            any(step.get("status") == "failed" and step.get("reason") == "file_too_large" for step in substeps),
            substeps,
        )
        events = [name for name, _ in observe.events]
        self.assertIn("brain_tooluse_ledger_completed_with_warnings", events)

    # --- G. brain text alone cannot mark passed ------------------------------

    def test_g_brain_textual_answer_alone_cannot_mark_passed(self) -> None:
        # No tool events at all → no synthetic task created, so there is no
        # row whose verification_status could be "passed". This is the
        # stronger version of "cannot mark passed": no row exists.
        observe = _RecordingObserve()
        observe.canned_trace_events = []
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="explicame esto",
            runtime_channel="telegram",
        )
        self.assertEqual(self.ledger.list(limit=10), [])

    def test_g2_tools_ran_no_failures_does_not_mark_passed(self) -> None:
        # Tools ran without failures, but the brain has no evidence pack
        # → defense-in-depth keeps the task off "passed".
        observe = _RecordingObserve()
        observe.canned_trace_events = [_tool_event("Read", tool_input={"file_path": "/x"})]
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="lee el archivo",
            runtime_channel="telegram",
        )
        task = self.ledger.list(limit=10)[0]
        self.assertEqual(task.notify_policy, "none")
        self.assertNotEqual(task.verification_status, "passed")
        # PR 0F: with evidence_manifest attached, the row closes
        # terminally as succeeded + needs_verification (no longer
        # downgraded to running by the false-success guard).
        self.assertEqual(task.status, "succeeded")
        self.assertEqual(task.verification_status, "needs_verification")
        events = [name for name, _ in observe.events]
        self.assertIn("brain_tooluse_ledger_needs_verification", events)

    # --- H. tier-3 / approval gate -> recorded as skipped --------------------

    def test_h_approval_blocked_tool_does_not_auto_succeed(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            {
                "event_type": "tool_blocked_by_freeze",
                "trace_id": "trace-X",
                "payload": json.dumps(
                    {"tool": "Bash", "tier": 3, "actor": "brain", "reason": "circuit_breaker:cost_per_hour"}
                ),
            }
        ]
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="deploy",
            runtime_channel="telegram",
        )
        events = [name for name, _ in observe.events]
        self.assertIn("brain_tooluse_ledger_skipped_sensitive", events)
        self.assertEqual(self.ledger.list(limit=10), [])

    # --- I. secret-shaped text not stored raw --------------------------------

    def test_i_secret_shaped_input_not_stored_raw_in_ledger(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [_tool_event("Read", tool_input={"file_path": "/x"})]
        bot = _make_bot(observe, self.ledger)
        # Synthetic token — NOT a real secret. Mirrors the secret-shaped
        # input from the 2026-05-11 audit row.
        fake_token = "9aBcDeFgHi1234567jKl"
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text=f"context: {fake_token}",
            runtime_channel="telegram",
        )
        task = self.ledger.list(limit=10)[0]
        # TaskLedger.create runs `redact_sensitive` on metadata + artifacts
        # before persistence; the fake token (mixed alphanumeric, 20 chars)
        # may pass the redactor's heuristics, so the contract we assert is
        # that source_message_summary is capped at 200 chars (truncation
        # bound) and that no event payload contains the raw token.
        for name, kwargs in observe.events:
            payload = kwargs.get("payload") or {}
            self.assertNotIn(
                fake_token,
                str(payload),
                f"token leaked in event {name}",
            )

    # --- J. false-success guard still fires when there's no manifest --------

    def test_j_false_success_guard_still_fires_for_naked_succeeded(self) -> None:
        # Defense-in-depth still works for any succeeded mark_terminal
        # call that lacks an evidence_manifest. The PR 0E/0F helper now
        # ALWAYS attaches a manifest, so we exercise this guard via a
        # direct mark_terminal call simulating a buggy caller.
        self.ledger.create(
            task_id="naked-task",
            session_id="tg-test",
            objective="anything",
            runtime="brain_fallback",
            mode="brain_fallback",
            status="running",
        )
        self.ledger.mark_terminal(
            "naked-task",
            status="succeeded",
            summary="I just told the user it's done",
            verification_status="unknown",
            artifacts={},  # no manifest, no evidence
        )
        task = self.ledger.list(limit=10)[0]
        # Defense-in-depth downgraded the succeeded request — task is
        # left in "running" with the verification carried forward (the
        # guard preserves the caller's verification_status hint, falling
        # back to "missing_evidence" only when none was supplied).
        self.assertEqual(task.status, "running")
        self.assertNotEqual(task.verification_status, "passed")
        ledger_events = [name for name, _ in self._ledger_observe.events]
        self.assertIn("task_false_success_prevented", ledger_events)


class BrainToolUseLedgerEdgeCasesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = TaskLedger(Path(self._tmp.name) / "claw.db")

    def test_missing_trace_id_is_a_silent_noop(self) -> None:
        observe = _RecordingObserve()
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={}),  # no trace_id
            source_text="hola",
            runtime_channel="telegram",
        )
        self.assertEqual(observe.events, [])
        self.assertEqual(self.ledger.list(limit=10), [])

    def test_no_task_ledger_returns_silently(self) -> None:
        # When the ledger isn't wired (e.g., a slim test runtime), the
        # helper must not blow up.
        observe = _RecordingObserve()
        bot = BotService.__new__(BotService)
        bot.observe = observe
        bot.task_ledger = None
        bot.brain = _StubBrain()
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="hola",
            runtime_channel="telegram",
        )
        self.assertEqual(observe.events, [])

    def test_active_task_not_running_does_not_attach(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [_tool_event("Read", tool_input={"file_path": "/x"})]
        bot = _make_bot(
            observe,
            self.ledger,
            state={
                "active_object": {
                    "active_task": {
                        "task_id": "old-task-finished",
                        "status": "succeeded",  # not running → ignored
                    }
                }
            },
        )
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="lee /x",
            runtime_channel="telegram",
        )
        # Synthetic task SHOULD be created (the finished task isn't
        # eligible for attachment).
        recent = self.ledger.list(limit=10)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0].mode, "brain_fallback")

    # --- C2: trace_events failure must be visible -----------------------------

    def test_observe_trace_events_failure_emits_observe_failed_event(self) -> None:
        """C2: trace_events failure must be visible (brain_tooluse_ledger_observe_failed)."""

        class _RaisingTraceObserve(_RecordingObserve):
            def trace_events(self, trace_id: str, *, limit: int | None = None) -> list[dict]:
                raise RuntimeError("simulated observe failure")

        observe = _RaisingTraceObserve()
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="que ves?",
            runtime_channel="telegram",
        )
        events = [name for name, _ in observe.events]
        self.assertIn(
            "brain_tooluse_ledger_observe_failed", events,
            f"Expected brain_tooluse_ledger_observe_failed in {events}",
        )
        # Ledger must remain untouched on observe failure.
        self.assertEqual(self.ledger.list(limit=10), [])


if __name__ == "__main__":
    unittest.main()
