from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from claw_v2.skills import SkillRegistry


class _RecordingObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **kwargs: object) -> None:
        payload = kwargs.get("payload")
        self.events.append((event_type, payload if isinstance(payload, dict) else {}))


class SkillRegistryTests(unittest.TestCase):
    @staticmethod
    def _safe_router() -> SimpleNamespace:
        return SimpleNamespace(
            ask=lambda *args, **kwargs: SimpleNamespace(
                content="""{
                    "name": "safe_skill",
                    "description": "adds numbers",
                    "function_name": "safe_skill",
                    "code": "def safe_skill(x: int = 1, y: int = 2):\\n    import math\\n    return {\\"result\\": math.floor(x + y)}"
                }"""
            )
        )

    def test_generate_skill_rejects_unsafe_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = SkillRegistry(
                root=Path(tmp),
                router=SimpleNamespace(
                    ask=lambda *args, **kwargs: SimpleNamespace(
                        content="""{
                            "name": "unsafe_skill",
                            "description": "bad",
                            "function_name": "unsafe_skill",
                            "code": "def unsafe_skill():\\n    import os\\n    return {\\"result\\": os.listdir(\\"/\\")}"
                        }"""
                    )
                ),
            )

            result = registry.generate_skill(task_description="do something unsafe")

            self.assertFalse(result["success"])
            self.assertIn("Unsafe skill rejected", result["error"])
            self.assertFalse((Path(tmp) / "unsafe_skill.py").exists())

    def test_generate_skill_registers_pending_review_by_default(self) -> None:
        observe = _RecordingObserve()
        with tempfile.TemporaryDirectory() as tmp:
            registry = SkillRegistry(root=Path(tmp), router=self._safe_router(), observe=observe)

            created = registry.generate_skill(task_description="add numbers safely")

            self.assertTrue(created["success"])
            self.assertEqual(created["status"], "pending_review")
            self.assertTrue(created["requires_review"])
            self.assertEqual(created["size_bytes"], (Path(tmp) / "safe_skill.py").stat().st_size)
            self.assertRegex(created["sha256_hash"], r"^[0-9a-f]{64}$")
            self.assertEqual(registry.list_skills()[0]["status"], "pending_review")
            self.assertEqual(registry.stats()["pending_review"], 1)
            events = [name for name, _ in observe.events]
            self.assertIn("codeskill_governance_allowed", events)

    def test_pending_review_skill_cannot_execute_without_activation(self) -> None:
        observe = _RecordingObserve()
        with tempfile.TemporaryDirectory() as tmp:
            registry = SkillRegistry(root=Path(tmp), router=self._safe_router(), observe=observe)
            registry.generate_skill(task_description="add numbers safely")

            executed = registry.execute_skill("safe_skill", x=3, y=4)

            self.assertFalse(executed["success"])
            self.assertTrue(executed["blocked"])
            self.assertEqual(executed["reason"], "skill_status_pending_review_not_executable")
            denied = [payload for name, payload in observe.events if name == "codeskill_governance_denied"]
            self.assertTrue(denied)
            self.assertEqual(denied[-1]["action"], "execute")
            self.assertTrue(denied[-1]["requires_approval"])

    def test_execute_skill_runs_in_restricted_subprocess(self) -> None:
        observe = _RecordingObserve()
        with tempfile.TemporaryDirectory() as tmp:
            registry = SkillRegistry(
                root=Path(tmp),
                router=self._safe_router(),
                observe=observe,
            )

            created = registry.generate_skill(task_description="add numbers safely")
            registry._registry["safe_skill"].status = "active"
            registry._save_registry()
            executed = registry.execute_skill("safe_skill", x=3, y=4)

            self.assertTrue(created["success"])
            self.assertTrue(executed["success"])
            self.assertEqual(executed["result"]["result"], 7)
            self.assertEqual(registry.list_skills()[0]["use_count"], 1)
            allowed = [payload for name, payload in observe.events if name == "codeskill_governance_allowed"]
            self.assertEqual(allowed[-1]["action"], "execute")

    def test_generate_skill_blocks_sensitive_task_before_router(self) -> None:
        observe = _RecordingObserve()

        class Router:
            def ask(self, *args, **kwargs):
                raise AssertionError("router should not be called for sensitive CodeSkill target")

        with tempfile.TemporaryDirectory() as tmp:
            registry = SkillRegistry(root=Path(tmp), router=Router(), observe=observe)

            result = registry.generate_skill(task_description="edit .env and rotate API key")

            self.assertFalse(result["success"])
            self.assertTrue(result["blocked"])
            self.assertTrue(result["requires_approval"])
            self.assertEqual(result["reason"], "sensitive_generation_target_requires_approval")
            self.assertEqual(registry.list_skills(), [])
            denied = [payload for name, payload in observe.events if name == "codeskill_governance_denied"]
            self.assertEqual(denied[-1]["action"], "generate")

    def test_generate_skill_blocks_dotenv_target_before_router(self) -> None:
        class Router:
            def ask(self, *args, **kwargs):
                raise AssertionError("router should not be called")

        with tempfile.TemporaryDirectory() as tmp:
            registry = SkillRegistry(root=Path(tmp), router=Router())

            result = registry.generate_skill(task_description="edit .env file")

            self.assertFalse(result["success"])
            self.assertTrue(result["blocked"])
            self.assertEqual(result["reason"], "sensitive_generation_target_requires_approval")

    def test_generate_skill_blocks_agents_md_target_before_router(self) -> None:
        class Router:
            def ask(self, *args, **kwargs):
                raise AssertionError("router should not be called")

        with tempfile.TemporaryDirectory() as tmp:
            registry = SkillRegistry(root=Path(tmp), router=Router())

            result = registry.generate_skill(task_description="update AGENTS.md instructions")

            self.assertFalse(result["success"])
            self.assertTrue(result["blocked"])
            self.assertEqual(result["reason"], "sensitive_generation_target_requires_approval")

    def test_governance_event_does_not_emit_raw_sensitive_tags(self) -> None:
        observe = _RecordingObserve()

        class Router:
            def ask(self, *args, **kwargs):
                raise AssertionError("router should not be called")

        with tempfile.TemporaryDirectory() as tmp:
            registry = SkillRegistry(root=Path(tmp), router=Router(), observe=observe)

            registry.generate_skill(
                task_description="create utility",
                tags=["sk-secret-value"],
            )

            serialized = json.dumps(observe.events)
            self.assertNotIn("sk-secret-value", serialized)
            denied = [payload for name, payload in observe.events if name == "codeskill_governance_denied"]
            self.assertEqual(denied[-1]["tag_count"], 1)
            self.assertNotIn("tags", denied[-1])
            self.assertRegex(denied[-1]["tag_sha256"][0], r"^[0-9a-f]{64}$")

    def test_generate_skill_blocks_path_traversal_name_without_writing(self) -> None:
        observe = _RecordingObserve()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            registry = SkillRegistry(
                root=root,
                observe=observe,
                router=SimpleNamespace(
                    ask=lambda *args, **kwargs: SimpleNamespace(
                        content="""{
                            "name": "../evil",
                            "description": "bad path",
                            "function_name": "evil",
                            "code": "def evil():\\n    return {\\"result\\": 1}"
                        }"""
                    )
                ),
            )

            result = registry.generate_skill(task_description="create utility")

            self.assertFalse(result["success"])
            self.assertTrue(result["blocked"])
            self.assertEqual(result["reason"], "invalid_skill_name")
            self.assertFalse((Path(tmp) / "evil.py").exists())
            self.assertFalse((root / "../evil.py").exists())

    def test_generate_skill_does_not_update_existing_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = SkillRegistry(root=Path(tmp), router=self._safe_router())
            first = registry.generate_skill(task_description="add numbers safely")
            original_code = (Path(tmp) / "safe_skill.py").read_text(encoding="utf-8")
            registry.router = SimpleNamespace(
                ask=lambda *args, **kwargs: SimpleNamespace(
                    content="""{
                        "name": "safe_skill",
                        "description": "tries to replace existing",
                        "function_name": "safe_skill",
                        "code": "def safe_skill():\\n    return {\\"result\\": 999}"
                    }"""
                )
            )

            second = registry.generate_skill(task_description="replace safe skill")

            self.assertTrue(first["success"])
            self.assertFalse(second["success"])
            self.assertIn("already exists", second["error"])
            self.assertEqual((Path(tmp) / "safe_skill.py").read_text(encoding="utf-8"), original_code)

    def test_discover_gaps_passes_evidence_pack_to_judge_lane(self) -> None:
        calls: list[dict] = []

        class Router:
            def ask(self, *args, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(
                    content='[{"name": "summarize_logs", "description": "Summarize logs", "task_description": "Create a log summarizer"}]'
                )

        with tempfile.TemporaryDirectory() as tmp:
            registry = SkillRegistry(root=Path(tmp), router=Router())

            result = registry.discover_gaps()

            self.assertEqual(result["gaps"][0]["name"], "summarize_logs")
            self.assertEqual(calls[0]["lane"], "judge")
            self.assertEqual(calls[0]["evidence_pack"]["operation"], "skill_gap_discovery")
            self.assertEqual(calls[0]["evidence_pack"]["active_skill_count"], 0)


if __name__ == "__main__":
    unittest.main()
