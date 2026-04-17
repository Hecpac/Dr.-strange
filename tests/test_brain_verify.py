from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.adapters.base import LLMRequest
from claw_v2.main import build_runtime
from claw_v2.types import LLMResponse


class BrainVerificationTests(unittest.TestCase):
    @staticmethod
    def _fake_brain(request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content="handled:brain",
            lane=request.lane,
            provider="anthropic",
            model=request.model,
        )

    def test_verify_critical_action_creates_approval_for_high_risk_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "VERIFIER_PROVIDER": "openai",
                "VERIFIER_MODEL": "gpt-5.4-mini",
            }

            def verifier_transport(request: LLMRequest) -> LLMResponse:
                self.assertEqual(request.lane, "verifier")
                return LLMResponse(
                    content=(
                        '{"recommendation":"needs_approval","risk_level":"high","summary":"'
                        'Migration touches production deploy path.","reasons":["Affects release path"],'
                        '"blockers":["No canary run"],"missing_checks":["Need rollback test"],"confidence":0.82}'
                    ),
                    lane="verifier",
                    provider="openai",
                    model=request.model,
                    confidence=0.82,
                    cost_estimate=0.03,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=self._fake_brain, openai_transport=verifier_transport)
                verdict = runtime.brain.verify_critical_action(
                    plan="Ship deployment refactor",
                    diff="diff --git a/deploy.sh b/deploy.sh",
                    test_output="unit ok",
                    action="deploy_production",
                )

                self.assertEqual(verdict.recommendation, "needs_approval")
                self.assertEqual(verdict.risk_level, "high")
                self.assertFalse(verdict.should_proceed)
                self.assertTrue(verdict.requires_human_approval)
                self.assertIsNotNone(verdict.approval_id)
                self.assertIsNotNone(verdict.approval_token)
                payload = runtime.approvals.read(verdict.approval_id)
                self.assertEqual(payload["action"], "deploy_production")
                self.assertEqual(payload["metadata"]["risk_level"], "high")
                self.assertIn("consensus_status", payload["metadata"])
                self.assertEqual(len(payload["metadata"]["verifier_votes"]), 2)
                recent = runtime.observe.recent_events(limit=5)
                self.assertTrue(any(event["event_type"] == "critical_action_verification" for event in recent))

    def test_verify_critical_action_allows_clean_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "VERIFIER_PROVIDER": "openai",
                "VERIFIER_MODEL": "gpt-5.4-mini",
            }

            def verifier_transport(request: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    content=(
                        '{"recommendation":"approve","risk_level":"low","summary":"Ready to proceed.",'
                        '"reasons":["Tests passed"],"blockers":[],"missing_checks":[],"confidence":0.91}'
                    ),
                    lane="verifier",
                    provider="openai",
                    model=request.model,
                    confidence=0.91,
                    cost_estimate=0.01,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=self._fake_brain, openai_transport=verifier_transport)
                verdict = runtime.brain.verify_critical_action(
                    plan="Ship docs-only change",
                    diff="README update",
                    test_output="not required",
                )
                self.assertEqual(verdict.recommendation, "approve")
                self.assertTrue(verdict.should_proceed)
                self.assertFalse(verdict.requires_human_approval)
                self.assertIsNone(verdict.approval_id)
                self.assertEqual(verdict.consensus_status, "unanimous_approve")
                self.assertEqual(len(verdict.verifier_votes), 2)

    def test_verify_critical_action_requires_approval_on_verifier_disagreement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "VERIFIER_PROVIDER": "openai",
                "VERIFIER_MODEL": "gpt-5.4-mini",
            }

            def openai_verifier(request: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    content=(
                        '{"recommendation":"approve","risk_level":"low","summary":"Safe.",'
                        '"reasons":["Tests passed"],"blockers":[],"missing_checks":[],"confidence":0.9}'
                    ),
                    lane="verifier",
                    provider="openai",
                    model=request.model,
                )

            def anthropic_verifier(request: LLMRequest) -> LLMResponse:
                if request.lane != "verifier":
                    return self._fake_brain(request)
                return LLMResponse(
                    content=(
                        '{"recommendation":"deny","risk_level":"high","summary":"Unsafe.",'
                        '"reasons":["Missing rollback"],"blockers":["No rollback"],"missing_checks":[],"confidence":0.8}'
                    ),
                    lane="verifier",
                    provider="anthropic",
                    model=request.model,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=anthropic_verifier, openai_transport=openai_verifier)
                verdict = runtime.brain.verify_critical_action(
                    plan="Deploy prod",
                    diff="deploy.sh",
                    test_output="unit ok",
                    action="deploy_prod",
                )

                self.assertEqual(verdict.recommendation, "needs_approval")
                self.assertEqual(verdict.consensus_status, "disagreement")
                self.assertTrue(verdict.requires_human_approval)
                self.assertFalse(verdict.should_proceed)
                self.assertIsNotNone(verdict.approval_id)

    def test_verify_critical_action_falls_back_to_text_heuristics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "VERIFIER_PROVIDER": "openai",
                "VERIFIER_MODEL": "gpt-5.4-mini",
            }

            def verifier_transport(request: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    content="Human review required before proceed. High risk because no rollback evidence.",
                    lane="verifier",
                    provider="openai",
                    model=request.model,
                    confidence=0.5,
                    cost_estimate=0.01,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=self._fake_brain, openai_transport=verifier_transport)
                verdict = runtime.brain.verify_critical_action(
                    plan="Touch prod migration",
                    diff="migration.sql",
                    test_output="unknown",
                    create_approval=False,
                )
                self.assertEqual(verdict.recommendation, "needs_approval")
                self.assertEqual(verdict.risk_level, "high")
                self.assertTrue(verdict.requires_human_approval)
                self.assertFalse(verdict.should_proceed)
                self.assertIsNone(verdict.approval_id)

    def test_execute_critical_action_runs_immediately_when_verifier_approves(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "VERIFIER_PROVIDER": "openai",
                "VERIFIER_MODEL": "gpt-5.4-mini",
            }

            def verifier_transport(request: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    content=(
                        '{"recommendation":"approve","risk_level":"low","summary":"Ready.",'
                        '"reasons":["Safe"],"blockers":[],"missing_checks":[],"confidence":0.95}'
                    ),
                    lane="verifier",
                    provider="openai",
                    model=request.model,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=self._fake_brain, openai_transport=verifier_transport)
                executed: list[str] = []
                result = runtime.brain.execute_critical_action(
                    action="deploy_docs",
                    plan="Deploy docs",
                    diff="README",
                    test_output="n/a",
                    executor=lambda: executed.append("ok") or {"done": True},
                )
                self.assertTrue(result.executed)
                self.assertEqual(result.status, "executed")
                self.assertEqual(executed, ["ok"])

    def test_execute_critical_action_waits_for_approval_and_then_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "VERIFIER_PROVIDER": "openai",
                "VERIFIER_MODEL": "gpt-5.4-mini",
            }

            def verifier_transport(request: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    content=(
                        '{"recommendation":"needs_approval","risk_level":"high","summary":"Need human review.",'
                        '"reasons":["Prod change"],"blockers":["No canary"],"missing_checks":[],"confidence":0.7}'
                    ),
                    lane="verifier",
                    provider="openai",
                    model=request.model,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=self._fake_brain, openai_transport=verifier_transport)
                executed: list[str] = []
                pending = runtime.brain.execute_critical_action(
                    action="deploy_prod",
                    plan="Deploy prod",
                    diff="deploy.sh",
                    test_output="unit ok",
                    executor=lambda: executed.append("ran"),
                )
                self.assertFalse(pending.executed)
                self.assertEqual(pending.status, "awaiting_approval")
                self.assertEqual(executed, [])

                runtime.approvals.approve(pending.verification.approval_id, pending.verification.approval_token)
                approved = runtime.brain.execute_critical_action(
                    action="deploy_prod",
                    plan="Deploy prod",
                    diff="deploy.sh",
                    test_output="unit ok",
                    executor=lambda: executed.append("ran") or {"done": True},
                    approval_id=pending.verification.approval_id,
                )
                self.assertTrue(approved.executed)
                self.assertEqual(approved.status, "executed_with_approval")
                self.assertEqual(executed, ["ran"])


if __name__ == "__main__":
    unittest.main()
