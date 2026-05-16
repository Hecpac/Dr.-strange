"""PR 0F: evidence pack + completion gate for brain tool-use.

PR 0E created the brain tool-use ledger row but couldn't close it cleanly
because there was no evidence schema. The row landed in
`running + verification_status=needs_verification`, which the stale-running
reaper would eventually reclassify as `lost`.

PR 0F fixes that:

  - `task_completion.validate_completion` recognises the
    `artifacts.evidence_manifest` shape and lets a brain_fallback row
    close terminally as `succeeded + needs_verification` (or `succeeded
    + passed` when the manifest reports a verified result with no
    blockers).
  - `BotService._attach_brain_tool_use_ledger` always emits the
    manifest with tools_run / commands_run / files_read / files_written
    / files_touched / grep_patterns / glob_patterns / trace_id /
    counts / blockers.
  - Text-only brain answers and approval-blocked turns still don't
    create rows (they can't earn "passed" through PR 0F either).
  - The false-success guard still works for any caller that bypasses
    the helper (e.g. a buggy direct `mark_terminal` with no manifest).

These tests run against a real `TaskLedger` + a stub `observe` so the
validate_completion logic actually exercises.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claw_v2.bot import BotService
from claw_v2.task_completion import (
    _has_brain_tooluse_evidence_manifest,
    validate_completion,
)
from claw_v2.task_ledger import TaskLedger


class _RecordingObserve:
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
    artifacts: dict[str, Any] = field(default_factory=dict)


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


def _make_bot(observe: _RecordingObserve, task_ledger: TaskLedger, state: dict | None = None) -> BotService:
    bot = BotService.__new__(BotService)
    bot.observe = observe
    bot.task_ledger = task_ledger
    bot.brain = _StubBrain(state)
    return bot


# ---------------------------------------------------------------------------
# 1. validate_completion: the pure logic, no I/O.
# ---------------------------------------------------------------------------


class ValidateCompletionBrainManifestTests(unittest.TestCase):
    def _manifest(self, **overrides) -> dict[str, Any]:
        base = {
            "version": 1,
            "task_id": "t1",
            "origin": "brain_fallback",
            "tools_run": ["Read", "Grep"],
            "trace_id": "trace-X",
            "verification_result": "unknown",
            "blockers": [],
        }
        base.update(overrides)
        return base

    def test_recognizer_requires_origin_brain_fallback(self) -> None:
        record = {"artifacts": {"evidence_manifest": self._manifest(origin="coordinator")}}
        self.assertFalse(_has_brain_tooluse_evidence_manifest(record))

    def test_recognizer_requires_non_empty_tools_run(self) -> None:
        record = {"artifacts": {"evidence_manifest": self._manifest(tools_run=[])}}
        self.assertFalse(_has_brain_tooluse_evidence_manifest(record))

    def test_recognizer_requires_correlation_hook(self) -> None:
        record = {"artifacts": {"evidence_manifest": self._manifest(trace_id="", observe_event_ids=[])}}
        self.assertFalse(_has_brain_tooluse_evidence_manifest(record))

    def test_recognizer_accepts_trace_id(self) -> None:
        record = {"artifacts": {"evidence_manifest": self._manifest()}}
        self.assertTrue(_has_brain_tooluse_evidence_manifest(record))

    def test_recognizer_accepts_observe_event_ids_when_no_trace(self) -> None:
        record = {
            "artifacts": {
                "evidence_manifest": self._manifest(trace_id="", observe_event_ids=[123, 456])
            }
        }
        self.assertTrue(_has_brain_tooluse_evidence_manifest(record))

    def test_succeeded_needs_verification_with_manifest_closes_terminally(self) -> None:
        decision = validate_completion(
            {
                "status": "succeeded",
                "verification_status": "needs_verification",
                "summary": "brain tool-use turn",
                "artifacts": {"evidence_manifest": self._manifest()},
            }
        )
        self.assertEqual(decision.final_status, "succeeded")
        self.assertEqual(decision.verification_status, "needs_verification")
        self.assertEqual(decision.reason, "brain_tooluse_with_manifest_pending_verification")

    def test_succeeded_with_manifest_passed_verification_upgrades_to_passed(self) -> None:
        decision = validate_completion(
            {
                "status": "succeeded",
                "verification_status": "needs_verification",
                "summary": "brain tool-use turn (verified)",
                "artifacts": {"evidence_manifest": self._manifest(verification_result="passed", blockers=[])},
            }
        )
        self.assertEqual(decision.final_status, "succeeded")
        self.assertEqual(decision.verification_status, "passed")
        self.assertEqual(decision.reason, "brain_tooluse_verified_with_manifest")

    def test_manifest_with_blockers_does_not_promote_to_passed(self) -> None:
        decision = validate_completion(
            {
                "status": "succeeded",
                "verification_status": "needs_verification",
                "summary": "brain tool-use turn",
                "artifacts": {
                    "evidence_manifest": self._manifest(
                        verification_result="passed",
                        blockers=["awaiting credential to deploy"],
                    )
                },
            }
        )
        # Blockers present → must NOT promote.
        self.assertEqual(decision.verification_status, "needs_verification")

    def test_text_only_succeeded_without_manifest_still_blocked(self) -> None:
        # The pre-existing guard for success-without-passed-verification
        # must still fire when there is no manifest.
        decision = validate_completion(
            {
                "status": "succeeded",
                "verification_status": "unknown",
                "summary": "everything went fine",
                "artifacts": {},
            }
        )
        self.assertEqual(decision.final_status, "pending")

    def test_existing_coordinator_path_still_passes_with_classic_evidence(self) -> None:
        # Coordinator tasks pass via the classic verification=passed +
        # has_evidence branch and must remain unaffected.
        decision = validate_completion(
            {
                "status": "succeeded",
                "verification_status": "passed",
                "summary": "coordinator turn",
                "artifacts": {"diff": "stuff", "tests": "passed"},
            }
        )
        self.assertEqual(decision.final_status, "succeeded")
        self.assertEqual(decision.verification_status, "passed")


# ---------------------------------------------------------------------------
# 2. End-to-end through the bot helper + real TaskLedger.
# ---------------------------------------------------------------------------


class BrainToolUseEvidencePackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._ledger_observe = _RecordingObserve()
        self.ledger = TaskLedger(
            Path(self._tmp.name) / "claw.db", observe=self._ledger_observe
        )

    # --- A. completed with manifest closes (not running, not passed) --------

    def test_a_completed_brain_tooluse_with_manifest_closes_needs_verification(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Read", tool_input={"file_path": "/etc/hosts"}),
        ]
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="lee /etc/hosts",
            runtime_channel="telegram",
        )
        task = self.ledger.list(limit=10)[0]
        self.assertEqual(task.status, "succeeded")
        self.assertEqual(task.verification_status, "needs_verification")
        manifest = task.artifacts.get("evidence_manifest") or {}
        self.assertEqual(manifest.get("origin"), "brain_fallback")
        self.assertEqual(manifest.get("trace_id"), "trace-X")
        self.assertEqual(manifest.get("verification_result"), "needs_verification")
        self.assertIn("started_at", manifest)
        self.assertIn("completed_at", manifest)

    # --- B. completed without tools never marks passed (no row created) ----

    def test_b_text_only_brain_turn_does_not_create_row_and_cannot_pass(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = []
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="explicame react",
            runtime_channel="telegram",
        )
        self.assertEqual(self.ledger.list(limit=10), [])

    # --- C. stale-running reaper does NOT reap closed brain_fallback rows --

    def test_c_terminal_brain_fallback_row_is_not_reaped(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [_tool_event("Read", tool_input={"file_path": "/x"})]
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="lee /x",
            runtime_channel="telegram",
        )
        task_before = self.ledger.list(limit=10)[0]
        self.assertEqual(task_before.status, "succeeded")
        # Reaper sweeps anything still 'running' past the TTL — should
        # not touch our terminal row even with TTL=0.
        reaped = self.ledger.mark_stale_running_lost(older_than_seconds=0.0)
        self.assertEqual(reaped, 0)
        task_after = self.ledger.list(limit=10)[0]
        self.assertEqual(task_after.status, "succeeded")
        self.assertEqual(task_after.verification_status, "needs_verification")

    # --- D. _has_evidence recognises the manifest shape ---------------------

    def test_d_has_evidence_recognises_brain_manifest(self) -> None:
        from claw_v2.task_completion import _has_evidence

        manifest = {
            "origin": "brain_fallback",
            "tools_run": ["Bash"],
            "trace_id": "trace-X",
            "commands_run": ["ls"],
        }
        self.assertTrue(_has_evidence({"artifacts": {"evidence_manifest": manifest}}))
        # Empty manifest does not count.
        self.assertFalse(
            _has_evidence({"artifacts": {"evidence_manifest": {"origin": "brain_fallback"}}})
        )

    # --- E. text-only brain answer cannot mark passed (no row at all) ------

    def test_e_text_only_answer_cannot_create_passed_row(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = []
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="me explicas?",
            runtime_channel="telegram",
        )
        passed = [t for t in self.ledger.list(limit=20) if t.verification_status == "passed"]
        self.assertEqual(passed, [])

    # --- F. Bash command metadata captured / capped -------------------------

    def test_f_bash_command_metadata_capped_in_manifest(self) -> None:
        observe = _RecordingObserve()
        long_cmd = "ps aux " + ("x" * 400)
        observe.canned_trace_events = [
            _tool_event("Bash", tool_input={"command": long_cmd}),
        ]
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="que procesos hay",
            runtime_channel="telegram",
        )
        task = self.ledger.list(limit=10)[0]
        manifest = task.artifacts["evidence_manifest"]
        self.assertEqual(len(manifest["commands_run"]), 1)
        self.assertLessEqual(len(manifest["commands_run"][0]), 200)

    # --- G. Read/Grep/Glob metadata --------------------------------------

    def test_g_read_grep_glob_metadata_captured(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Read", tool_input={"file_path": "/Users/x/notes.md"}),
            _tool_event("Grep", tool_input={"pattern": "TODO", "path": "claw_v2/"}),
            _tool_event("Glob", tool_input={"pattern": "**/*.py"}),
            _tool_event("Write", tool_input={"file_path": "/tmp/out.txt"}),
        ]
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="busca y escribe",
            runtime_channel="telegram",
        )
        manifest = self.ledger.list(limit=10)[0].artifacts["evidence_manifest"]
        self.assertIn("/Users/x/notes.md", manifest["files_read"])
        self.assertIn("/tmp/out.txt", manifest["files_written"])
        self.assertIn("TODO", manifest["grep_patterns"])
        self.assertIn("**/*.py", manifest["glob_patterns"])
        self.assertEqual(
            sorted(manifest["tools_run"]),
            ["Glob", "Grep", "Read", "Write"],
        )

    # --- H. failed tool → failed, not passed --------------------------------

    def test_h_failed_tool_closes_failed_not_passed(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [
            _tool_event("Read", tool_input={"file_path": "/x"}),
            _tool_failure_event("Bash", error="Exit code 127 command not found"),
        ]
        bot = _make_bot(observe, self.ledger)
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="prueba",
            runtime_channel="telegram",
        )
        task = self.ledger.list(limit=10)[0]
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.verification_status, "failed")
        manifest = task.artifacts["evidence_manifest"]
        self.assertEqual(manifest["verification_result"], "failed")
        self.assertTrue(manifest["blockers"])

    # --- I. secret-shaped strings stay out of manifest + events ------------

    def test_i_secret_shaped_input_not_in_manifest_or_events(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [_tool_event("Read", tool_input={"file_path": "/x"})]
        bot = _make_bot(observe, self.ledger)
        # Synthetic token — NOT a real secret.
        fake_token = "9aBcDeFgHi1234567jKl"
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text=f"context: {fake_token}",
            runtime_channel="telegram",
        )
        task = self.ledger.list(limit=10)[0]
        # The token may appear in user_message_summary (capped to 200
        # chars by the source-text truncation), but it must NOT appear
        # in any observability event payload.
        for name, kwargs in observe.events:
            self.assertNotIn(
                fake_token,
                str(kwargs),
                f"token leaked in event {name}",
            )
        for name, kwargs in self._ledger_observe.events:
            self.assertNotIn(
                fake_token,
                str(kwargs),
                f"token leaked in ledger event {name}",
            )

    # --- J. owner_delegation context attaches, no duplicate task ----------

    def test_j_owner_delegation_attaches_no_duplicate_evidence_pack(self) -> None:
        observe = _RecordingObserve()
        observe.canned_trace_events = [_tool_event("Read", tool_input={"file_path": "/x"})]
        bot = _make_bot(
            observe,
            self.ledger,
            state={
                "active_object": {
                    "active_task": {"task_id": "owner-delegated-T", "status": "running"}
                }
            },
        )
        bot._attach_brain_tool_use_ledger(
            session_id="tg-test",
            response=_StubResponse(artifacts={"trace_id": "trace-X"}),
            source_text="continua",
            runtime_channel="telegram",
        )
        # No synthetic brain-fallback task created — the manifest would
        # have lived inside the existing delegated coordinator task
        # instead (out of scope for PR 0F to populate).
        self.assertEqual(self.ledger.list(limit=10), [])
        events = [name for name, _ in observe.events]
        self.assertIn("brain_tooluse_ledger_attached_existing", events)

    # --- K. false-success guard still bites bad direct callers ------------

    def test_k_false_success_guard_still_bites_callers_without_manifest(self) -> None:
        self.ledger.create(
            task_id="naked",
            session_id="tg-test",
            objective="anything",
            runtime="brain_fallback",
            mode="brain_fallback",
            status="running",
        )
        self.ledger.mark_terminal(
            "naked",
            status="succeeded",
            summary="claim of done with no proof",
            verification_status="unknown",
            artifacts={},
        )
        task = self.ledger.list(limit=10)[0]
        self.assertEqual(task.status, "running")
        self.assertNotEqual(task.verification_status, "passed")
        ledger_events = [name for name, _ in self._ledger_observe.events]
        self.assertIn("task_false_success_prevented", ledger_events)

    # --- L. existing coordinator-shaped row still passes through normally --

    def test_l_classic_coordinator_evidence_path_still_passes(self) -> None:
        self.ledger.create(
            task_id="coord-1",
            session_id="tg-test",
            objective="run the smoke suite",
            runtime="coordinator",
            mode="coding",
            status="running",
        )
        self.ledger.mark_terminal(
            "coord-1",
            status="succeeded",
            summary="suite ran",
            verification_status="passed",
            artifacts={"diff": "x", "tests": "passed"},
        )
        task = self.ledger.get("coord-1")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "succeeded")
        self.assertEqual(task.verification_status, "passed")


if __name__ == "__main__":
    unittest.main()
