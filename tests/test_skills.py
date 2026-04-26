from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from claw_v2.skills import SkillRegistry


class SkillRegistryTests(unittest.TestCase):
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

    def test_execute_skill_runs_in_restricted_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = SkillRegistry(
                root=Path(tmp),
                router=SimpleNamespace(
                    ask=lambda *args, **kwargs: SimpleNamespace(
                        content="""{
                            "name": "safe_skill",
                            "description": "adds numbers",
                            "function_name": "safe_skill",
                            "code": "def safe_skill(x: int = 1, y: int = 2):\\n    import math\\n    return {\\"result\\": math.floor(x + y)}"
                        }"""
                    )
                ),
            )

            created = registry.generate_skill(task_description="add numbers safely")
            executed = registry.execute_skill("safe_skill", x=3, y=4)

            self.assertTrue(created["success"])
            self.assertTrue(executed["success"])
            self.assertEqual(executed["result"]["result"], 7)
            self.assertEqual(registry.list_skills()[0]["use_count"], 1)

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
