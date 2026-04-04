from __future__ import annotations

import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.dream import AutoDreamService, DreamResult


def _make_service(**overrides):
    memory = MagicMock()
    observe = MagicMock()
    router = MagicMock()
    defaults = dict(
        memory=memory,
        observe=observe,
        router=router,
        min_hours_between_dreams=0.0,
        min_sessions_between_dreams=1,
        max_facts=10,
    )
    defaults.update(overrides)
    svc = AutoDreamService(**defaults)
    return svc, memory, observe, router


class ShouldDreamTests(unittest.TestCase):
    def test_should_dream_after_time_elapsed(self) -> None:
        svc, *_ = _make_service(min_hours_between_dreams=0.0)
        should, reason = svc.should_dream()
        self.assertTrue(should)
        self.assertEqual(reason, "time_elapsed")

    def test_should_not_dream_recently(self) -> None:
        svc, *_ = _make_service(min_hours_between_dreams=24.0, min_sessions_between_dreams=100)
        svc._last_dream_at = time.time()
        svc._sessions_since_dream = 0
        should, _ = svc.should_dream()
        self.assertFalse(should)

    def test_should_dream_after_sessions(self) -> None:
        svc, *_ = _make_service(min_hours_between_dreams=999, min_sessions_between_dreams=3)
        svc._last_dream_at = time.time()
        svc._sessions_since_dream = 3
        should, reason = svc.should_dream()
        self.assertTrue(should)
        self.assertEqual(reason, "session_count")

    def test_tick_session_increments(self) -> None:
        svc, *_ = _make_service()
        self.assertEqual(svc._sessions_since_dream, 0)
        svc.tick_session()
        svc.tick_session()
        self.assertEqual(svc._sessions_since_dream, 2)


class OrientTests(unittest.TestCase):
    def test_orient_returns_facts(self) -> None:
        svc, memory, *_ = _make_service()
        memory.search_facts.return_value = [{"key": "a", "value": "b"}]
        facts = svc._orient()
        self.assertEqual(len(facts), 1)
        memory.search_facts.assert_called_once_with("", limit=20)


class GatherSignalTests(unittest.TestCase):
    def test_gather_filters_relevant_events(self) -> None:
        svc, _, observe, _ = _make_service()
        observe.recent_events.return_value = [
            {"event_type": "llm_response", "payload": {}},
            {"event_type": "random_event", "payload": {}},
            {"event_type": "morning_brief", "payload": {}},
        ]
        signals = svc._gather_signal()
        self.assertEqual(len(signals), 2)

    def test_gather_empty_events(self) -> None:
        svc, _, observe, _ = _make_service()
        observe.recent_events.return_value = []
        signals = svc._gather_signal()
        self.assertEqual(len(signals), 0)


class ConsolidateTests(unittest.TestCase):
    def test_consolidate_empty_facts(self) -> None:
        svc, *_ = _make_service()
        result = svc._consolidate([], [])
        self.assertEqual(result, 0)

    def test_consolidate_parses_actions(self) -> None:
        svc, memory, _, router = _make_service()
        router.ask.return_value = MagicMock(content='[{"action": "delete", "key": "old_fact"}, {"action": "create", "key": "new_fact", "value": "hello"}]')
        # search_facts must return the existing fact so the verified delete path finds it
        memory.search_facts.return_value = [{"key": "old_fact", "value": "stale", "confidence": 0.5, "source": "user"}]
        facts = [{"key": "old_fact", "value": "stale", "confidence": 0.5, "source": "user"}]
        result = svc._consolidate(facts, [])
        self.assertEqual(result, 2)
        self.assertEqual(memory.store_fact.call_count, 2)
        # Verify router.ask was called with evidence_pack
        call_kwargs = router.ask.call_args
        self.assertIn("evidence_pack", call_kwargs.kwargs)
        self.assertEqual(call_kwargs.kwargs["lane"], "research")

    def test_consolidate_handles_empty_response(self) -> None:
        svc, _, _, router = _make_service()
        router.ask.return_value = MagicMock(content="[]")
        facts = [{"key": "a", "value": "b"}]
        result = svc._consolidate(facts, [])
        self.assertEqual(result, 0)

    def test_consolidate_handles_bad_json(self) -> None:
        svc, _, _, router = _make_service()
        router.ask.return_value = MagicMock(content="not json at all")
        facts = [{"key": "a", "value": "b"}]
        result = svc._consolidate(facts, [])
        self.assertEqual(result, 0)


class PruneTests(unittest.TestCase):
    def test_prune_under_limit_does_nothing(self) -> None:
        svc, memory, *_ = _make_service(max_facts=10)
        memory.search_facts.return_value = [{"key": f"k{i}", "value": "v", "confidence": 0.5} for i in range(5)]
        pruned = svc._prune()
        self.assertEqual(pruned, 0)

    def test_prune_over_limit_removes_lowest_confidence(self) -> None:
        svc, memory, *_ = _make_service(max_facts=3)
        memory.search_facts.return_value = [
            {"key": "low", "value": "v", "confidence": 0.1},
            {"key": "mid", "value": "v", "confidence": 0.5},
            {"key": "high", "value": "v", "confidence": 0.9},
            {"key": "extra", "value": "v", "confidence": 0.2},
        ]
        pruned = svc._prune()
        self.assertEqual(pruned, 1)

    def test_prune_preserves_profile_facts(self) -> None:
        svc, memory, *_ = _make_service(max_facts=1)
        memory.search_facts.return_value = [
            {"key": "profile.name", "value": "Hector", "confidence": 0.1},
            {"key": "temp", "value": "v", "confidence": 0.2},
        ]
        pruned = svc._prune()
        self.assertEqual(pruned, 1)
        stored_key = memory.store_fact.call_args[0][0]
        self.assertEqual(stored_key, "temp")


class RunTests(unittest.TestCase):
    def test_run_skips_when_conditions_not_met(self) -> None:
        svc, *_ = _make_service(min_hours_between_dreams=999, min_sessions_between_dreams=999)
        svc._last_dream_at = time.time()
        result = svc.run()
        self.assertTrue(result.skipped)
        self.assertEqual(result.reason, "conditions_not_met")

    @patch("claw_v2.dream._acquire_lock", return_value=False)
    def test_run_skips_when_lock_held(self, _mock_lock) -> None:
        svc, *_ = _make_service(min_hours_between_dreams=0.0)
        result = svc.run()
        self.assertTrue(result.skipped)
        self.assertEqual(result.reason, "lock_held")

    @patch("claw_v2.dream._release_lock")
    @patch("claw_v2.dream._acquire_lock", return_value=True)
    def test_run_full_cycle(self, _mock_lock, _mock_release) -> None:
        svc, memory, observe, router = _make_service(min_hours_between_dreams=0.0)
        memory.search_facts.return_value = [{"key": "a", "value": "b", "confidence": 0.5, "source": "user"}]
        observe.recent_events.return_value = []
        router.ask.return_value = MagicMock(content="[]")

        result = svc.run()

        self.assertFalse(result.skipped)
        self.assertGreater(result.duration_seconds, 0)
        observe.emit.assert_called_once()
        self.assertEqual(svc._sessions_since_dream, 0)

    @patch("claw_v2.dream._release_lock")
    @patch("claw_v2.dream._acquire_lock", return_value=True)
    def test_run_resets_session_counter(self, _mock_lock, _mock_release) -> None:
        svc, memory, observe, router = _make_service(min_hours_between_dreams=0.0)
        svc._sessions_since_dream = 10
        memory.search_facts.return_value = []
        observe.recent_events.return_value = []
        router.ask.return_value = MagicMock(content="[]")

        svc.run()
        self.assertEqual(svc._sessions_since_dream, 0)


class ExportSharedTests(unittest.TestCase):
    def test_exports_high_confidence_facts(self) -> None:
        import json
        import tempfile
        svc, memory, observe, _ = _make_service()
        svc.agent_name = "rook"
        svc.shared_memory_root = Path(tempfile.mkdtemp())
        facts = [
            {"key": "cron.conflict", "value": "SEO vs health", "confidence": 0.8, "source": "rook"},
            {"key": "low.fact", "value": "meh", "confidence": 0.3, "source": "rook"},
        ]
        count = svc._export_shared(facts)
        self.assertEqual(count, 1)
        export_file = svc.shared_memory_root / "rook_exports.jsonl"
        self.assertTrue(export_file.exists())
        lines = export_file.read_text().strip().split("\n")
        self.assertEqual(len(lines), 1)
        data = json.loads(lines[0])
        self.assertEqual(data["key"], "cron.conflict")
        self.assertEqual(data["source_agent"], "rook")


class ImportSharedTests(unittest.TestCase):
    def test_imports_matching_tags(self) -> None:
        import json
        import tempfile
        shared_root = Path(tempfile.mkdtemp())
        export = {"key": "cron.issue", "value": "conflict", "source_agent": "rook", "confidence": 0.8, "timestamp": 1000, "tags": ["infra", "cron"]}
        (shared_root / "rook_exports.jsonl").write_text(json.dumps(export) + "\n")
        svc, memory, _, _ = _make_service()
        svc.agent_name = "hex"
        svc.shared_memory_root = shared_root
        svc.import_tags = ["infra", "cron", "code"]
        memory.search_facts.return_value = []
        imported = svc._import_shared()
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0]["key"], "cron.issue")

    def test_skips_personal_tags_for_non_alma(self) -> None:
        import json
        import tempfile
        shared_root = Path(tempfile.mkdtemp())
        export = {"key": "personal.pref", "value": "morning meetings", "source_agent": "alma", "confidence": 0.9, "timestamp": 1000, "tags": ["personal"]}
        (shared_root / "alma_exports.jsonl").write_text(json.dumps(export) + "\n")
        svc, memory, _, _ = _make_service()
        svc.agent_name = "hex"
        svc.shared_memory_root = shared_root
        svc.import_tags = ["personal", "code"]
        memory.search_facts.return_value = []
        imported = svc._import_shared()
        self.assertEqual(len(imported), 0)

    def test_alma_can_import_personal(self) -> None:
        import json
        import tempfile
        shared_root = Path(tempfile.mkdtemp())
        export = {"key": "personal.pref", "value": "morning meetings", "source_agent": "rook", "confidence": 0.9, "timestamp": 1000, "tags": ["personal"]}
        (shared_root / "rook_exports.jsonl").write_text(json.dumps(export) + "\n")
        svc, memory, _, _ = _make_service()
        svc.agent_name = "alma"
        svc.shared_memory_root = shared_root
        svc.import_tags = ["personal"]
        memory.search_facts.return_value = []
        imported = svc._import_shared()
        self.assertEqual(len(imported), 1)

    def test_skips_own_exports(self) -> None:
        import json
        import tempfile
        shared_root = Path(tempfile.mkdtemp())
        export = {"key": "self.fact", "value": "mine", "source_agent": "hex", "confidence": 0.9, "timestamp": 1000, "tags": ["code"]}
        (shared_root / "hex_exports.jsonl").write_text(json.dumps(export) + "\n")
        svc, memory, _, _ = _make_service()
        svc.agent_name = "hex"
        svc.shared_memory_root = shared_root
        svc.import_tags = ["code"]
        memory.search_facts.return_value = []
        imported = svc._import_shared()
        self.assertEqual(len(imported), 0)


if __name__ == "__main__":
    unittest.main()
