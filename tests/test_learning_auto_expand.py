"""Wave 3.4: auto-expand skills from failure clusters.

LearningLoop scans recent task_outcomes, groups failures by tag, and
asks SkillRegistry to generate a skill for any cluster that meets the
minimum size threshold. Each candidate can be gated through Evaluator
before adoption — bad clusters don't ship bad skills.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.learning import LearningLoop
from claw_v2.memory import MemoryStore


class _RecordingObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **kwargs: object) -> None:
        self.events.append((event_type, dict(kwargs)))


class _FakeEvalResult:
    def __init__(self, passed: bool) -> None:
        self.passed = passed
        self.failures: list[str] = []


class _FakeEvaluator:
    def __init__(self, passed: bool = True) -> None:
        self._passed = passed

    def run_self_improvement_gate(self, *, plan, diff, test_output):
        return _FakeEvalResult(self._passed)


def _seed_failures(memory: MemoryStore, *, tag: str, count: int) -> None:
    for i in range(count):
        memory.store_task_outcome(
            task_type="self_heal",
            task_id=f"{tag}-{i}",
            description=f"failure case {i} for {tag}",
            approach="approach",
            outcome="failure",
            lesson="learn",
            error_snippet="boom",
            retries=0,
            tags=[tag],
        )


class DetectFailureClustersTests(unittest.TestCase):
    def test_groups_failures_by_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "m.db")
            _seed_failures(memory, tag="chrome_cdp_login", count=6)
            _seed_failures(memory, tag="vercel_token_expired", count=3)
            loop = LearningLoop(memory=memory)

            clusters = loop.detect_failure_clusters(min_cluster_size=5)

            self.assertIn("chrome_cdp_login", clusters)
            self.assertEqual(len(clusters["chrome_cdp_login"]), 6)
            self.assertNotIn("vercel_token_expired", clusters)


class AutoExpandSkillsTests(unittest.TestCase):
    def _setup(self, observe: _RecordingObserve | None = None):
        tmp = Path(tempfile.mkdtemp())
        memory = MemoryStore(tmp / "m.db")
        loop = LearningLoop(memory=memory, observe=observe)
        return tmp, memory, loop

    def test_generates_skill_for_each_cluster_above_threshold(self) -> None:
        observe = _RecordingObserve()
        _, memory, loop = self._setup(observe)
        _seed_failures(memory, tag="chrome_cdp_login", count=5)
        skill_registry = MagicMock()
        skill_registry.generate_skill.return_value = {
            "success": True,
            "name": "chrome_login_handler",
        }

        result = loop.auto_expand_skills(skill_registry=skill_registry)

        self.assertEqual(result["clusters_processed"], 1)
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(result["results"][0]["applied"])
        skill_registry.generate_skill.assert_called_once()
        call_kwargs = skill_registry.generate_skill.call_args.kwargs
        self.assertIn("chrome_cdp_login", call_kwargs["task_description"])
        self.assertEqual(call_kwargs["tags"], ["chrome_cdp_login"])
        events = [name for name, _ in observe.events]
        self.assertIn("skill_auto_expanded", events)

    def test_skips_when_gate_denies(self) -> None:
        observe = _RecordingObserve()
        _, memory, loop = self._setup(observe)
        _seed_failures(memory, tag="bad_tag", count=5)
        skill_registry = MagicMock()
        evaluator = _FakeEvaluator(passed=False)

        result = loop.auto_expand_skills(skill_registry=skill_registry, evaluator=evaluator)

        skill_registry.generate_skill.assert_not_called()
        self.assertFalse(result["results"][0]["applied"])
        self.assertEqual(result["results"][0]["reason"], "gate_denied")
        self.assertNotIn("skill_auto_expanded", [n for n, _ in observe.events])

    def test_records_failure_when_skill_generation_returns_error(self) -> None:
        _, memory, loop = self._setup()
        _seed_failures(memory, tag="unstable_provider", count=5)
        skill_registry = MagicMock()
        skill_registry.generate_skill.return_value = {
            "success": False,
            "error": "router_unavailable",
        }

        result = loop.auto_expand_skills(skill_registry=skill_registry)

        self.assertEqual(result["results"][0]["applied"], False)
        self.assertEqual(result["results"][0]["reason"], "router_unavailable")

    def test_records_failure_when_skill_generation_raises(self) -> None:
        _, memory, loop = self._setup()
        _seed_failures(memory, tag="boom_tag", count=5)
        skill_registry = MagicMock()
        skill_registry.generate_skill.side_effect = RuntimeError("LLM outage")

        result = loop.auto_expand_skills(skill_registry=skill_registry)

        self.assertEqual(result["results"][0]["applied"], False)
        self.assertTrue(result["results"][0]["reason"].startswith("generation_error"))

    def test_returns_empty_when_no_cluster_meets_threshold(self) -> None:
        _, memory, loop = self._setup()
        _seed_failures(memory, tag="just_two", count=2)
        skill_registry = MagicMock()

        result = loop.auto_expand_skills(skill_registry=skill_registry, min_cluster_size=5)

        self.assertEqual(result["clusters_processed"], 0)
        self.assertEqual(result["results"], [])
        skill_registry.generate_skill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
