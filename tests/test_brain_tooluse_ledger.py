"""PR 0E: brain tool-use ledger.

The brain fallback can invoke tools via `router.ask(lane="brain")`.
Before PR 0E, those tool calls left no `agent_tasks` row, so audits
saw "brain said done" with zero durable trace.

PR 0E wraps every brain fallback turn in a post-hoc ledger pass:

  - 0 tool events                  → noop, no task created (no DB noise)
  - any tool event + active task   → attach (no second task)
  - any tool event + no active task→ synthetic agent_tasks row with
                                      status="completed_unverified" and
                                      verification_status="needs_verification"
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
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from claw_v2.bot import BotService
from claw_v2.jobs import JobService
from claw_v2.notebooklm import NotebookLMService
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


class _StubTaskHandler:
    def __init__(self, coordinator: object | None = None) -> None:
        self.coordinator = coordinator

    def _lane_model_overrides(self, session_id: str) -> dict[str, dict[str, Any]]:
        return {}


@dataclass
class _StubResponse:
    content: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)


def _make_bot(
    observe: _RecordingObserve,
    task_ledger: TaskLedger,
    state: dict | None = None,
    job_service: JobService | None = None,
    brain_tooluse_verify: bool = False,
    coordinator: object | None = None,
) -> BotService:
    """Construct just enough BotService surface for the ledger to run.

    BotService has a heavy __init__ (loads MCP/agents/etc.) so we
    instantiate via __new__ and attach the three attributes
    `_attach_brain_tool_use_ledger` actually reads.
    """
    bot = BotService.__new__(BotService)
    bot.observe = observe
    bot.task_ledger = task_ledger
    bot.job_service = job_service
    bot.brain = _StubBrain(state)
    bot.config = SimpleNamespace(brain_tooluse_verify=brain_tooluse_verify)
    bot._task_handler = _StubTaskHandler(coordinator)
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
        self.assertEqual(task.status, "completed_unverified")
        self.assertEqual(task.verification_status, "needs_verification")
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
        # Runtime invariant: a manifest records tool activity, but the row closes
        # unverified until a verifier promotes it to passed success.
        self.assertEqual(task.status, "completed_unverified")
        self.assertEqual(task.verification_status, "needs_verification")
        outcome = task.artifacts["outcome_manifest"]
        self.assertEqual(outcome["final_outcome"], "needs_verification")
        self.assertEqual(outcome["pending_async_jobs"], [])
        self.assertEqual(outcome["blockers"], [])
        events = [name for name, _ in observe.events]
        self.assertIn("brain_tooluse_ledger_needs_verification", events)

    def test_g3_external_action_without_verifier_blocks_instead_of_completed_unverified(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Write", tool_input={"file_path": "artifacts/instagram/_publish_reel.py"}),
            _tool_event(
                "Bash",
                tool_input={"command": "python artifacts/instagram/_publish_reel.py"},
            ),
        ]
        bot = _make_bot(observe, self.ledger)

        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="Publicalo",
            runtime_channel="telegram",
        )

        task = self.ledger.list(limit=10)[0]
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.verification_status, "blocked")
        self.assertIn("passed verification", task.error)
        manifest = task.artifacts["evidence_manifest"]
        self.assertEqual(manifest["verification_result"], "blocked")
        self.assertIn("passed_verification_missing_for_action", manifest["blockers"])
        outcome = task.artifacts["outcome_manifest"]
        self.assertEqual(outcome["final_outcome"], "blocked")
        self.assertIn("passed_verification_missing_for_action", outcome["blockers"])
        events = [name for name, _ in observe.events]
        self.assertIn("brain_tooluse_ledger_blocked_unverified_action", events)
        self.assertNotIn("brain_tooluse_ledger_needs_verification", events)

    def test_telegram_runtime_defers_brain_tooluse_ledger_before_delivery(self) -> None:
        observe = _RecordingObserve()
        bot = _make_bot(observe, self.ledger)
        started: list[dict[str, Any]] = []

        class _FakeThread:
            def __init__(self, *, target, name: str, daemon: bool) -> None:
                self.target = target
                self.name = name
                self.daemon = daemon

            def start(self) -> None:
                started.append({"name": self.name, "daemon": self.daemon})

        with patch("claw_v2.bot.threading.Thread", _FakeThread):
            deferred = bot._defer_brain_tool_use_ledger(
                session_id="tg-test",
                response=_StubResponse(artifacts={"trace_id": "trace-X"}),
                source_text="Haz un repaso por X",
                runtime_channel="telegram",
            )

        self.assertTrue(deferred)
        self.assertEqual(started, [{"name": "brain-tooluse-ledger-trace-X", "daemon": True}])
        self.assertEqual(self.ledger.list(limit=10), [])
        events = [name for name, _ in observe.events]
        self.assertIn("brain_tooluse_ledger_deferred", events)

    def test_non_telegram_runtime_keeps_brain_tooluse_ledger_inline(self) -> None:
        observe = _RecordingObserve()
        bot = _make_bot(observe, self.ledger)

        deferred = bot._defer_brain_tool_use_ledger(
            session_id="web-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="Haz un repaso por X",
            runtime_channel="web",
        )

        self.assertFalse(deferred)

    def test_g4_external_status_check_without_verifier_blocks_even_with_read_tool(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Read", tool_input={"file_path": "artifacts/instagram/check_publish_profile.png"}),
        ]
        bot = _make_bot(observe, self.ledger)

        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="Publicaste ?",
            runtime_channel="telegram",
        )

        task = self.ledger.list(limit=10)[0]
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.verification_status, "blocked")
        self.assertEqual(
            task.artifacts["evidence_manifest"]["blockers"],
            ["passed_verification_missing_for_action"],
        )

    def test_instagram_readonly_review_with_browser_evidence_closes_verified_readonly(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Bash", tool_input={"command": "python3 artifacts/ig_feed/_ig_feed_sweep.py"}),
            _tool_event("Read", tool_input={"file_path": "artifacts/ig_feed/ig_feed_1780891885_top.png"}),
            _tool_event("Read", tool_input={"file_path": "artifacts/ig_feed/ig_feed_1780891885.json"}),
        ]
        bot = _make_bot(observe, self.ledger)

        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(
                content="Revisé el feed y extraje evidencia visible con screenshot y DOM.",
                artifacts={"trace_id": "trace-X"},
            ),
            source_text="Abre Instagram y dale un repaso por el feed y consigue tips para mejorar el setup",
            runtime_channel="telegram",
        )

        task = self.ledger.list(limit=10)[0]
        self.assertEqual(task.status, "succeeded")
        self.assertEqual(task.verification_status, "passed")
        self.assertEqual(task.artifacts["evidence_manifest"]["verification_result"], "passed_readonly")
        self.assertEqual(task.artifacts["outcome_manifest"]["final_outcome"], "passed")

    def test_b_mutation_without_action_text_blocks_even_with_verifier_off(self) -> None:
        # PR2 Checkpoint B: the blocker now fires on executed mutation
        # (files_written / commands_run), not only on the 6 request-text regex.
        # With the verifier flag OFF, a Write/Edit/Bash turn whose text does NOT
        # match still closes blocked, not benign completed_unverified.
        # Supersedes the prior flag-off-conservative behavior (the former
        # test_b1_off_is_unchanged_for_mutation); INTERNAL_WIRING
        # brain_tooluse_verify_flag_gated is updated in the same commit.
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Write", tool_input={"file_path": "notes/b1.txt"}),
        ]
        bot = _make_bot(observe, self.ledger, brain_tooluse_verify=False, coordinator=object())

        with patch("claw_v2.bot.verify_brain_tooluse") as verifier:
            bot._attach_brain_tool_use_ledger(
                session_id="tg-test",
                response=_StubResponse(artifacts={"trace_id": "trace-X"}),
                source_text="actualiza la nota",
                runtime_channel="telegram",
            )

        verifier.assert_not_called()  # flag off -> no verifier call
        task = self.ledger.list(limit=10)[0]
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.verification_status, "blocked")
        events = [name for name, _ in observe.events]
        self.assertIn("brain_tooluse_ledger_blocked_unverified_action", events)
        self.assertNotIn("brain_tooluse_ledger_needs_verification", events)

    def test_b_edit_mutation_without_action_text_blocks(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Edit", tool_input={"file_path": "notes/b1.txt"}),
        ]
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="ajusta el texto",
            runtime_channel="telegram",
        )
        task = self.ledger.list(limit=10)[0]
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.verification_status, "blocked")

    def test_b_bash_mutation_without_action_text_blocks(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Bash", tool_input={"command": "mkdir -p build && touch build/x"}),
        ]
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="prepara el build",
            runtime_channel="telegram",
        )
        task = self.ledger.list(limit=10)[0]
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.verification_status, "blocked")

    def test_b_readonly_without_error_is_not_blocked_by_mutation(self) -> None:
        # His #4: read-only tools (no mutation, no action text) still close benign.
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Grep", tool_input={"pattern": "TODO"}),
            _tool_event("Glob", tool_input={"pattern": "**/*.py"}),
        ]
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="busca los TODO",
            runtime_channel="telegram",
        )
        task = self.ledger.list(limit=10)[0]
        self.assertEqual(task.status, "completed_unverified")
        self.assertEqual(task.verification_status, "needs_verification")
        self.assertEqual(task.artifacts["outcome_manifest"]["final_outcome"], "needs_verification")

    def test_b1_on_passed_closes_succeeded_for_mutation(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Write", tool_input={"file_path": "notes/b1.txt"}),
            _tool_event("Bash", tool_input={"command": "pytest -q"}),
        ]
        bot = _make_bot(
            observe,
            self.ledger,
            brain_tooluse_verify=True,
            coordinator=object(),
        )

        with patch("claw_v2.bot.verify_brain_tooluse", return_value="passed") as verifier:
            bot._attach_brain_tool_use_ledger(
                session_id="tg-test",
                response=_StubResponse(
                    content="Actualicé la nota y corrí pytest.",
                    artifacts={"trace_id": "trace-X"},
                ),
                source_text="actualiza la nota",
                runtime_channel="telegram",
            )

        verifier.assert_called_once()
        task = self.ledger.list(limit=10)[0]
        self.assertEqual(task.status, "succeeded")
        self.assertEqual(task.verification_status, "passed")
        self.assertEqual(task.artifacts["evidence_manifest"]["verification_result"], "passed")
        outcome = task.artifacts["outcome_manifest"]
        self.assertEqual(outcome["final_outcome"], "passed")
        self.assertEqual(outcome["blockers"], [])
        self.assertEqual(outcome["pending_async_jobs"], [])
        self.assertIn(
            {"kind": "brain_tooluse_verifier", "result": "passed"},
            outcome["verifications"],
        )
        events = [name for name, _ in observe.events]
        self.assertIn("brain_tooluse_ledger_verified", events)

    def test_b1_on_passed_closes_succeeded_for_keyword_required_read(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Read", tool_input={"file_path": "artifacts/instagram/check_publish_profile.png"}),
        ]
        bot = _make_bot(
            observe,
            self.ledger,
            brain_tooluse_verify=True,
            coordinator=object(),
        )

        with patch("claw_v2.bot.verify_brain_tooluse", return_value="passed"):
            bot._attach_brain_tool_use_ledger(
                session_id="tg-test",
                response=_StubResponse(
                    content="Sí, quedó publicado.",
                    artifacts={"trace_id": "trace-X"},
                ),
                source_text="Publicaste ?",
                runtime_channel="telegram",
            )

        task = self.ledger.list(limit=10)[0]
        self.assertEqual(task.status, "succeeded")
        self.assertEqual(task.verification_status, "passed")

    def test_b1_on_failed_closes_failed(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Write", tool_input={"file_path": "notes/b1.txt"}),
        ]
        bot = _make_bot(
            observe,
            self.ledger,
            brain_tooluse_verify=True,
            coordinator=object(),
        )

        with patch("claw_v2.bot.verify_brain_tooluse", return_value="failed") as verifier:
            bot._attach_brain_tool_use_ledger(
                session_id="tg-test",
                response=_StubResponse(artifacts={"trace_id": "trace-X"}),
                source_text="actualiza la nota",
                runtime_channel="telegram",
            )

        verifier.assert_called_once()
        task = self.ledger.list(limit=10)[0]
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.verification_status, "failed")
        self.assertEqual(task.artifacts["evidence_manifest"]["verification_result"], "failed")

    def test_b1_on_pending_mutation_blocks(self) -> None:
        # PR2-B: a pending verdict is not a pass; a mutation that did not earn a
        # passed verifier falls through to the now mutation-aware blocker.
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Write", tool_input={"file_path": "notes/b1.txt"}),
        ]
        bot = _make_bot(
            observe,
            self.ledger,
            brain_tooluse_verify=True,
            coordinator=object(),
        )

        with patch("claw_v2.bot.verify_brain_tooluse", return_value="pending") as verifier:
            bot._attach_brain_tool_use_ledger(
                session_id="tg-test",
                response=_StubResponse(artifacts={"trace_id": "trace-X"}),
                source_text="actualiza la nota",
                runtime_channel="telegram",
            )

        verifier.assert_called_once()
        task = self.ledger.list(limit=10)[0]
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.verification_status, "blocked")
        self.assertEqual(task.artifacts["evidence_manifest"]["verification_result"], "blocked")

    def test_b1_no_coordinator_skips_verifier_blocks_mutation(self) -> None:
        # PR2-B: with no coordinator the verifier cannot run; a mutation still must
        # not benign-close, so the blocker fires instead of completed_unverified.
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Write", tool_input={"file_path": "notes/b1.txt"}),
        ]
        bot = _make_bot(
            observe,
            self.ledger,
            brain_tooluse_verify=True,
            coordinator=None,
        )

        with patch("claw_v2.bot.verify_brain_tooluse") as verifier:
            bot._attach_brain_tool_use_ledger(
                session_id="tg-test",
                response=_StubResponse(artifacts={"trace_id": "trace-X"}),
                source_text="actualiza la nota",
                runtime_channel="telegram",
            )

        verifier.assert_not_called()
        task = self.ledger.list(limit=10)[0]
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.verification_status, "blocked")

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
        _task = self.ledger.list(limit=10)[0]
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

    def test_background_monitor_claim_is_rejected_without_durable_job(self) -> None:
        observe = _RecordingObserve()
        bot = _make_bot(observe, self.ledger)

        rendered = bot._enforce_background_monitor_contract(
            session_id="tg-test",
            user_text="Opcion A",
            content="Watcher corriendo en background. Te aviso cuando termine.",
            raw_content="Watcher corriendo en background. Te aviso cuando termine.",
        )

        self.assertIn("no quedó un monitor durable registrado", rendered)
        events = [name for name, _ in observe.events]
        self.assertIn("background_monitor_claim_rejected", events)

    def test_durable_dispatch_claim_is_rejected_without_durable_job(self) -> None:
        observe = _RecordingObserve()
        bot = _make_bot(observe, self.ledger)
        content = (
            "Va A+B encadenado.\n\n"
            "**Disclosure operativo:** voy a hacer dispatch durable para que "
            "esto sobreviva interrupciones. Cuando termine, te entrego el "
            "digest con links y screenshots.\n\n"
            "Disparo ahora."
        )

        rendered = bot._enforce_background_monitor_contract(
            session_id="tg-test",
            user_text="A y B",
            content=content,
            raw_content=content,
        )

        self.assertIn("no quedó un monitor durable registrado", rendered)
        self.assertNotIn("Disparo ahora", rendered)
        events = [name for name, _ in observe.events]
        self.assertIn("background_monitor_claim_rejected", events)
        self.assertNotIn("background_monitor_claim_stripped", events)

    def test_durable_dispatch_claim_is_promoted_to_real_task(self) -> None:
        # Causa 2: an unbacked durable-dispatch claim must be turned into a real
        # coordinator task (so the work runs and evidence exists) instead of
        # being replaced wholesale with the defensive template.
        observe = _RecordingObserve()
        bot = _make_bot(observe, self.ledger)

        started_calls: list[tuple[str, str]] = []

        def _fake_start(session_id: str, objective: str, *, source_text: str = "") -> str:
            started_calls.append((session_id, objective))
            self.ledger.create(
                task_id=f"{session_id}:promoted",
                session_id=session_id,
                objective=objective,
                runtime="coordinator",
                mode="research",
                status="running",
                notify_policy="done_only",
            )
            return (
                "Voy con eso. La dejo corriendo y te aviso cuando cierre.\n\n"
                f"Tarea autónoma iniciada: `{session_id}:promoted`\nModo: research"
            )

        bot._task_handler = SimpleNamespace(
            coordinator=object(), start_autonomous_task=_fake_start
        )
        # Objective resolution from session_state has its own tests; isolate the
        # promotion wiring here.
        bot._resolve_actionable_task_objective = (
            lambda text, *, state: ("Haz el barrido de noticias", "pending_action")
        )

        content = (
            "Va A+B encadenado.\n\n"
            "**Disclosure operativo:** voy a hacer dispatch durable para que esto "
            "sobreviva interrupciones. Cuando termine, te entrego el digest.\n\n"
            "Disparo ahora."
        )

        rendered = bot._enforce_background_monitor_contract(
            session_id="tg-test",
            user_text="A+B",
            content=content,
            raw_content=content,
        )

        # The promise is preserved because a real running task now backs it.
        self.assertEqual(rendered, content)
        self.assertEqual(len(started_calls), 1)
        events = [name for name, _ in observe.events]
        self.assertIn("background_monitor_dispatch_promoted", events)
        self.assertNotIn("background_monitor_claim_rejected", events)

    def test_durable_dispatch_promotion_uses_reply_context_for_terse_pick(self) -> None:
        # Causa 2 option 1: a terse pick like "A+B" that the real actionable
        # resolver cannot pin down still promotes, using the reply_context plan
        # the user was replying to as the objective. The real resolver runs here
        # (not stubbed) so the fallback path is exercised end-to-end.
        observe = _RecordingObserve()
        reply_plan = (
            "Va A+B encadenado: barrido X via Chrome CDP sobre x.com/home, "
            "barrido web con WebSearch, y sintesis cruzada en un digest."
        )
        state = {
            "active_object": {
                "reply_context": {
                    "text": reply_plan,
                    "source": "telegram_reply",
                    "created_at": time.time(),
                }
            }
        }
        bot = _make_bot(observe, self.ledger, state=state)

        started_objectives: list[str] = []

        def _fake_start(session_id: str, objective: str, *, source_text: str = "") -> str:
            started_objectives.append(objective)
            self.ledger.create(
                task_id=f"{session_id}:promoted",
                session_id=session_id,
                objective=objective,
                runtime="coordinator",
                mode="research",
                status="running",
                notify_policy="done_only",
            )
            return f"Tarea autónoma iniciada: `{session_id}:promoted`\nModo: research"

        bot._task_handler = SimpleNamespace(
            coordinator=object(), start_autonomous_task=_fake_start
        )

        content = (
            "Va A+B encadenado.\n\n"
            "voy a hacer dispatch durable para que esto sobreviva interrupciones. "
            "Cuando termine te entrego el digest.\n\nDisparo ahora."
        )

        rendered = bot._enforce_background_monitor_contract(
            session_id="tg-test",
            user_text="A+B",
            content=content,
            raw_content=content,
        )

        self.assertEqual(rendered, content)
        self.assertEqual(len(started_objectives), 1)
        self.assertIn("barrido X", started_objectives[0])
        events = [name for name, _ in observe.events]
        self.assertIn("background_monitor_dispatch_promoted", events)
        self.assertNotIn("background_monitor_claim_rejected", events)

    def test_durable_dispatch_promotion_skipped_falls_back_to_template(self) -> None:
        # When no actionable objective can be resolved, promotion is skipped and
        # the existing block-with-template behavior is preserved.
        observe = _RecordingObserve()
        bot = _make_bot(observe, self.ledger)
        bot._task_handler = SimpleNamespace(
            coordinator=object(),
            start_autonomous_task=lambda *a, **k: "should not be called",
        )
        bot._resolve_actionable_task_objective = lambda text, *, state: (None, "missing_context")

        content = (
            "Voy a hacer dispatch durable y cuando termine te entrego el digest.\n\n"
            "Disparo ahora."
        )

        rendered = bot._enforce_background_monitor_contract(
            session_id="tg-test",
            user_text="A+B",
            content=content,
            raw_content=content,
        )

        self.assertIn("no quedó un monitor durable registrado", rendered)
        events = [name for name, _ in observe.events]
        self.assertIn("background_monitor_dispatch_promotion_skipped", events)
        self.assertIn("background_monitor_claim_rejected", events)

    def test_background_monitor_claim_is_stripped_from_mixed_response(self) -> None:
        observe = _RecordingObserve()
        bot = _make_bot(observe, self.ledger)
        content = (
            "Perfecto. Mensaje enviado a Drew.\n\n"
            "**Lo guardé en contexto para retomar cuando responda:**\n"
            "- Equipo Samantha: Sting GU11 D Oldham\n"
            "- Pendiente: confirmar número de jersey disponible\n\n"
            "**Volviendo a lo que dejamos antes de Sting:**\n"
            "- Watcher de NotebookLM sigue corriendo en background — te aviso cuando termine podcast + informe\n"
            "- F3b.2 sigue bloqueado por Keychain de HeyGen\n"
            "- Thread X del martes listo para draftear\n\n"
            "¿Movés algo de eso o lo dejamos hasta que Drew responda?"
        )

        rendered = bot._enforce_background_monitor_contract(
            session_id="tg-test",
            user_text="Listo ya quedo",
            content=content,
            raw_content=content,
        )

        self.assertIn("Perfecto. Mensaje enviado a Drew.", rendered)
        self.assertIn("F3b.2 sigue bloqueado", rendered)
        self.assertNotIn("Watcher de NotebookLM", rendered)
        self.assertNotIn("no quedó un monitor durable registrado", rendered)
        events = [name for name, _ in observe.events]
        self.assertIn("background_monitor_claim_stripped", events)
        self.assertNotIn("background_monitor_claim_rejected", events)

    def test_background_monitor_claim_in_raw_trace_only_is_not_blocked(self) -> None:
        observe = _RecordingObserve()
        bot = _make_bot(observe, self.ledger)

        rendered = bot._enforce_background_monitor_contract(
            session_id="tg-test",
            user_text="Listo ya quedo",
            content="Perfecto. Queda así.",
            raw_content="<trace>Watcher de NotebookLM sigue corriendo en background.</trace>",
        )

        self.assertEqual(rendered, "Perfecto. Queda así.")
        self.assertEqual(observe.events, [])

    def test_cross_line_only_monitor_claim_is_not_nuked_to_template(self) -> None:
        # False-positive class Hector flagged: a useful conversational reply where
        # the monitor pattern only matches across lines (DOTALL) and no single
        # line is strippable. The reply must be preserved, not replaced wholesale.
        observe = _RecordingObserve()
        bot = _make_bot(observe, self.ledger)
        content = (
            "Listo, lo dejé anotado.\n\n"
            "Cuando el proceso\n"
            "termine te entrego el resultado."
        )

        rendered = bot._enforce_background_monitor_contract(
            session_id="tg-test",
            user_text="Listo ya quedo",
            content=content,
            raw_content=content,
        )

        self.assertEqual(rendered, content)
        self.assertNotIn("no quedó un monitor durable registrado", rendered)
        events = [name for name, _ in observe.events]
        self.assertNotIn("background_monitor_claim_rejected", events)
        self.assertIn("background_monitor_claim_kept_unisolated", events)

    def test_background_monitor_claim_allowed_with_related_active_job(self) -> None:
        observe = _RecordingObserve()
        jobs = JobService(Path(self._tmp.name) / "claw.db")
        job = jobs.enqueue(
            kind="notebooklm.orchestrate",
            payload={"session_id": "tg-test", "notebook_id": "nb1"},
            resume_key="notebooklm:orchestrate:nb1",
        )
        bot = _make_bot(observe, self.ledger, job_service=jobs)
        content = f"Watcher corriendo en background. Job: {job.job_id}. Te aviso cuando termine."

        rendered = bot._enforce_background_monitor_contract(
            session_id="tg-test",
            user_text="Opcion A",
            content=content,
            raw_content=content,
        )

        self.assertEqual(rendered, content)
        self.assertEqual(observe.events, [])

    def test_notebooklm_video_monitor_claim_registers_durable_job(self) -> None:
        observe = _RecordingObserve()
        jobs = JobService(Path(self._tmp.name) / "claw.db")
        notebooklm = NotebookLMService(job_service=jobs)
        bot = _make_bot(observe, self.ledger, job_service=jobs)
        bot._nlm_handler = SimpleNamespace(notebooklm=notebooklm)
        content = (
            "Video Overview disparado en NotebookLM. "
            "Notebook ID: 04e763b8-2488-4462-8693-bac2ecce4918. "
            "Generando resumen de video. Te aviso cuando termine."
        )

        rendered = bot._enforce_background_monitor_contract(
            session_id="tg-test",
            user_text="Opción A",
            content=content,
            raw_content=content,
        )

        self.assertEqual(rendered, content)
        jobs_list = jobs.list()
        self.assertEqual(len(jobs_list), 1)
        job = jobs_list[0]
        self.assertEqual(job.kind, "notebooklm.orchestrate")
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.payload["session_id"], "tg-test")
        self.assertEqual(job.payload["outputs"], ["video"])
        self.assertEqual(
            job.resume_key,
            "notebooklm:orchestrate:04e763b8-2488-4462-8693-bac2ecce4918",
        )
        events = [name for name, _ in observe.events]
        self.assertIn("background_monitor_auto_registered", events)
        self.assertNotIn("background_monitor_claim_rejected", events)


if __name__ == "__main__":
    unittest.main()
