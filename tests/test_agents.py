from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.agents import AgentDefinition, AutoResearchAgentService, ExperimentRecord, FileAgentStore, StagnationDetector, SubAgentService
from claw_v2.eval_mocks import build_test_router, scripted_experiment_runner

from tests.helpers import make_config


class AgentServiceTests(unittest.TestCase):
    def test_create_agent_and_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = make_config(root)
            router = build_test_router(config)
            store = FileAgentStore(config.agent_state_root)
            service = AutoResearchAgentService(
                router=router,
                store=store,
                experiment_runner=scripted_experiment_runner([]),
            )
            service.create_agent(
                AgentDefinition(
                    name="researcher-1",
                    agent_class="researcher",
                    instruction="Investigate regressions.",
                )
            )
            state = service.inspect("researcher-1")
            self.assertIn("WebSearch", state["allowed_tools"])
            self.assertNotIn("Write", state["allowed_tools"])
            output = service.dispatch("researcher-1", "summarize current state")
            self.assertIn("anthropic:worker", output)

    def test_run_until_reaches_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = make_config(root)
            router = build_test_router(config)
            store = FileAgentStore(config.agent_state_root)
            service = AutoResearchAgentService(
                router=router,
                store=store,
                experiment_runner=scripted_experiment_runner(
                    [
                        ExperimentRecord(1, 0.10, 0.05, "improved"),
                        ExperimentRecord(2, 0.22, 0.10, "improved"),
                    ]
                ),
            )
            service.create_agent(AgentDefinition(name="operator-1", agent_class="operator", instruction="Improve"))
            result = service.run_until("operator-1", max_experiments=5, target_metric=0.2)
            self.assertEqual(result.reason, "target_reached")
            self.assertEqual(result.experiments_run, 2)

    def test_run_loop_pauses_on_stagnation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = make_config(root)
            router = build_test_router(config)
            store = FileAgentStore(config.agent_state_root)
            detector = StagnationDetector(no_improvement_streak=2, baseline_min_experiments=2)
            runner = scripted_experiment_runner(
                [
                    ExperimentRecord(1, 0.10, 0.10, "regressed"),
                    ExperimentRecord(2, 0.10, 0.10, "regressed"),
                ]
            )
            service = AutoResearchAgentService(router=router, store=store, experiment_runner=runner, detector=detector)
            service.create_agent(AgentDefinition(name="deployer-1", agent_class="deployer", instruction="Ship"))
            result = service.run_loop("deployer-1", max_experiments=3)
            self.assertTrue(result.paused)
            self.assertEqual(result.reason, "stagnating")
            self.assertTrue(service.status("deployer-1").paused)

    def test_update_controls_persists_promotion_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = make_config(root)
            router = build_test_router(config)
            store = FileAgentStore(config.agent_state_root)
            service = AutoResearchAgentService(
                router=router,
                store=store,
                experiment_runner=scripted_experiment_runner([]),
            )
            service.create_agent(AgentDefinition(name="operator-2", agent_class="operator", instruction="Improve"))

            updated = service.update_controls(
                "operator-2",
                promote_on_improvement=True,
                commit_on_promotion=True,
                branch_on_promotion=True,
                promotion_commit_message="chore(claw): custom",
                promotion_branch_name="claw/operator-2/review",
            )

            self.assertTrue(updated["promote_on_improvement"])
            self.assertTrue(updated["commit_on_promotion"])
            self.assertTrue(updated["branch_on_promotion"])
            self.assertEqual(updated["promotion_commit_message"], "chore(claw): custom")
            self.assertEqual(updated["promotion_branch_name"], "claw/operator-2/review")
            persisted = service.inspect("operator-2")
            self.assertEqual(persisted["promotion_commit_message"], "chore(claw): custom")
            self.assertEqual(persisted["promotion_branch_name"], "claw/operator-2/review")

    def test_pause_resume_and_history_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = make_config(root)
            router = build_test_router(config)
            store = FileAgentStore(config.agent_state_root)
            service = AutoResearchAgentService(
                router=router,
                store=store,
                experiment_runner=scripted_experiment_runner([]),
            )
            service.create_agent(AgentDefinition(name="operator-3", agent_class="operator", instruction="Improve"))
            store.append_result("operator-3", ExperimentRecord(1, 0.10, 0.05, "improved"))
            store.append_result("operator-3", ExperimentRecord(2, 0.12, 0.10, "improved"))

            paused = service.pause("operator-3")
            self.assertTrue(paused["paused"])
            resumed = service.resume("operator-3")
            self.assertFalse(resumed["paused"])
            history = service.history("operator-3", limit=1)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].experiment_number, 2)
            self.assertIsNone(history[0].promotion_commit_sha)

    def test_create_agent_rejects_duplicate_or_invalid_definition(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = make_config(root)
            router = build_test_router(config)
            store = FileAgentStore(config.agent_state_root)
            service = AutoResearchAgentService(
                router=router,
                store=store,
                experiment_runner=scripted_experiment_runner([]),
            )
            service.create_agent(AgentDefinition(name="operator-4", agent_class="operator", instruction="Improve"))

            with self.assertRaises(FileExistsError):
                service.create_agent(AgentDefinition(name="operator-4", agent_class="operator", instruction="Again"))
            with self.assertRaises(ValueError):
                service.create_agent(AgentDefinition(name="../bad", agent_class="operator", instruction="Bad"))
            with self.assertRaises(ValueError):
                service.create_agent(AgentDefinition(name="bad-class", agent_class="judge", instruction="Bad"))


class SubAgentServiceTests(unittest.TestCase):
    def test_parse_model_from_explicit_model_line(self) -> None:
        hex_soul = (
            "# SOUL\n"
            "- **Model:** GPT-5.4 Codex\n"
            "You run on Codex because your job is code-native reasoning at speed.\n"
        )
        lux_soul = (
            "# SOUL\n"
            "- **Model:** GPT-5.4\n"
            "You run on Gemini because your job needs broad synthesis across large content volumes.\n"
        )

        self.assertEqual(SubAgentService._parse_model_from_soul(hex_soul), ("codex", "codex-mini-latest"))
        self.assertEqual(SubAgentService._parse_model_from_soul(lux_soul), ("openai", "gpt-5.4"))


if __name__ == "__main__":
    unittest.main()
