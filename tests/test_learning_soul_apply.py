"""Wave 3.1: tests for LearningLoop.apply_soul_updates / revert_soul_update.

Closes the self-improvement loop: proposals are no longer parked as facts —
high-priority suggestions can be applied to SOUL.md after passing
Evaluator.run_self_improvement_gate, with a backup written for clean revert.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.learning import LearningLoop
from claw_v2.memory import MemoryStore


class _FakeEvalResult:
    def __init__(self, passed: bool, failures: list[str] | None = None) -> None:
        self.passed = passed
        self.failures = failures or []


class _FakeEvaluator:
    def __init__(self, passed: bool = True, failures: list[str] | None = None) -> None:
        self._passed = passed
        self._failures = failures or []
        self.calls: list[dict] = []

    def run_self_improvement_gate(self, *, plan: str, diff: str, test_output: str) -> _FakeEvalResult:
        self.calls.append({"plan": plan, "diff": diff, "test_output": test_output})
        return _FakeEvalResult(self._passed, self._failures)


class _RecordingObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **kwargs: object) -> None:
        self.events.append((event_type, dict(kwargs)))


def _make_loop(tmpdir: Path, observe: _RecordingObserve | None = None) -> LearningLoop:
    store = MemoryStore(tmpdir / "memory.db")
    return LearningLoop(memory=store, observe=observe)


def _seed_soul(tmpdir: Path) -> Path:
    soul = tmpdir / "agent" / "SOUL.md"
    soul.parent.mkdir(parents=True, exist_ok=True)
    soul.write_text("# Soul\n\nOriginal content.\n", encoding="utf-8")
    return soul


def _high_proposal() -> dict:
    return {
        "summary": "agent retries proceed-class on stale state — reduce reliance",
        "suggestions": [
            {
                "section": "Self-healing",
                "change": "Reject stale task_queue entries older than 1h before resolving proceed-class.",
                "reason": "10 outcomes show stale resume causing re-execution of completed tasks.",
                "priority": "high",
                "evidence": ["proceed_class_stale_x10"],
            }
        ],
        "do_not_change": ["audit_trail invariant"],
    }


class ApplySoulUpdatesTests(unittest.TestCase):
    def test_appends_section_when_high_priority_suggestion_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            soul = _seed_soul(tmpdir)
            loop = _make_loop(tmpdir, observe=_RecordingObserve())

            result = loop.apply_soul_updates(
                agent_name="rook",
                soul_path=soul,
                proposal=_high_proposal(),
                evaluator=_FakeEvaluator(passed=True),
            )

            self.assertTrue(result["applied"])
            self.assertIsNotNone(result["event_id"])
            new_text = soul.read_text(encoding="utf-8")
            self.assertIn("Auto-applied lessons", new_text)
            self.assertIn("Self-healing", new_text)
            self.assertIn("Reject stale task_queue entries", new_text)
            self.assertIn("Original content.", new_text, "original content must be preserved")

    def test_skips_when_no_high_priority_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            soul = _seed_soul(tmpdir)
            loop = _make_loop(tmpdir)
            proposal = _high_proposal()
            proposal["suggestions"][0]["priority"] = "low"

            result = loop.apply_soul_updates(
                agent_name="rook",
                soul_path=soul,
                proposal=proposal,
            )

            self.assertFalse(result["applied"])
            self.assertEqual(result["reason"], "no_suggestions_at_priority")
            self.assertEqual(soul.read_text(encoding="utf-8"), "# Soul\n\nOriginal content.\n")

    def test_skips_and_emits_event_when_gate_denies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            soul = _seed_soul(tmpdir)
            observe = _RecordingObserve()
            loop = _make_loop(tmpdir, observe=observe)

            evaluator = _FakeEvaluator(passed=False, failures=["weakens audit_trail invariant"])
            result = loop.apply_soul_updates(
                agent_name="rook",
                soul_path=soul,
                proposal=_high_proposal(),
                evaluator=evaluator,
            )

            self.assertFalse(result["applied"])
            self.assertEqual(result["reason"], "gate_denied")
            self.assertEqual(soul.read_text(encoding="utf-8"), "# Soul\n\nOriginal content.\n")
            self.assertEqual(len(evaluator.calls), 1)
            event_names = [name for name, _ in observe.events]
            self.assertIn("soul_update_skipped", event_names)

    def test_emits_soul_updated_event_with_hashes_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            soul = _seed_soul(tmpdir)
            observe = _RecordingObserve()
            loop = _make_loop(tmpdir, observe=observe)

            result = loop.apply_soul_updates(
                agent_name="rook",
                soul_path=soul,
                proposal=_high_proposal(),
                evaluator=_FakeEvaluator(passed=True),
            )

            self.assertTrue(result["applied"])
            self.assertIsNotNone(result["backup_path"])
            backup_path = Path(result["backup_path"])
            self.assertTrue(backup_path.exists())
            self.assertEqual(
                backup_path.read_text(encoding="utf-8"),
                "# Soul\n\nOriginal content.\n",
            )
            updated_events = [
                payload for name, payload in observe.events if name == "soul_updated"
            ]
            self.assertEqual(len(updated_events), 1)
            event = updated_events[0]["payload"]
            self.assertEqual(event["agent_name"], "rook")
            self.assertEqual(event["before_hash"], result["before_hash"])
            self.assertEqual(event["after_hash"], result["after_hash"])
            self.assertNotEqual(event["before_hash"], event["after_hash"])
            self.assertIn("Self-healing", event["suggestions_applied"])

    def test_runs_without_evaluator_when_none_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            soul = _seed_soul(tmpdir)
            loop = _make_loop(tmpdir, observe=_RecordingObserve())
            result = loop.apply_soul_updates(
                agent_name="rook",
                soul_path=soul,
                proposal=_high_proposal(),
                evaluator=None,
            )
            self.assertTrue(result["applied"])

    def test_returns_soul_not_found_when_path_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            loop = _make_loop(tmpdir)
            result = loop.apply_soul_updates(
                agent_name="missing",
                soul_path=tmpdir / "nope" / "SOUL.md",
                proposal=_high_proposal(),
            )
            self.assertFalse(result["applied"])
            self.assertTrue(result["reason"].startswith("soul_not_found"))


class RevertSoulUpdateTests(unittest.TestCase):
    def test_revert_restores_from_backup_and_emits_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            soul = _seed_soul(tmpdir)
            observe = _RecordingObserve()
            loop = _make_loop(tmpdir, observe=observe)

            apply_result = loop.apply_soul_updates(
                agent_name="rook",
                soul_path=soul,
                proposal=_high_proposal(),
                evaluator=_FakeEvaluator(passed=True),
            )
            self.assertTrue(apply_result["applied"])
            self.assertNotEqual(
                soul.read_text(encoding="utf-8"),
                "# Soul\n\nOriginal content.\n",
            )

            revert = loop.revert_soul_update(
                soul_path=soul,
                backup_path=apply_result["backup_path"],
                event_id=apply_result["event_id"],
            )
            self.assertTrue(revert["reverted"])
            self.assertEqual(soul.read_text(encoding="utf-8"), "# Soul\n\nOriginal content.\n")
            event_names = [name for name, _ in observe.events]
            self.assertIn("soul_reverted", event_names)

    def test_revert_returns_not_found_when_backup_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            soul = _seed_soul(tmpdir)
            loop = _make_loop(tmpdir)
            result = loop.revert_soul_update(
                soul_path=soul,
                backup_path=tmpdir / "nope.SOUL.md",
                event_id="bogus",
            )
            self.assertFalse(result["reverted"])
            self.assertTrue(result["reason"].startswith("backup_not_found"))


if __name__ == "__main__":
    unittest.main()
