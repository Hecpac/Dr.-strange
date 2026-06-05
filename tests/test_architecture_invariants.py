from __future__ import annotations

import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.adapters.base import LLMRequest, LLMResponse
from claw_v2.config import AppConfig, ProviderRolePolicyError
from claw_v2.main import build_runtime
from claw_v2.scheduled_background_jobs import (
    KAIROS_TICK_JOB_KIND,
    PERF_OPTIMIZER_JOB_KIND,
    WIKI_RESEARCH_JOB_KIND,
    WIKI_SCRAPE_JOB_KIND,
)
from claw_v2.skill_expand_jobs import SKILL_EXPAND_JOB_KIND
from claw_v2.skills import CodeSkillGovernancePolicy, Skill
from claw_v2.workspace import StartupContextReport


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROLES = {"control_judge", "control_verifier", "critical_verifier"}
SLOW_SCHEDULER_AGENT_JOBS = {
    "kairos_tick": KAIROS_TICK_JOB_KIND,
    "wiki_research": WIKI_RESEARCH_JOB_KIND,
    "wiki_scrape": WIKI_SCRAPE_JOB_KIND,
    "perf_optimizer": PERF_OPTIMIZER_JOB_KIND,
    "skill_expand": SKILL_EXPAND_JOB_KIND,
}


class ArchitectureInvariantTests(unittest.TestCase):
    def test_known_slow_scheduler_jobs_enqueue_agent_jobs_and_return_without_inline_work(self) -> None:
        def fake_anthropic(req: LLMRequest) -> LLMResponse:
            return LLMResponse(content="<response>ok</response>", lane=req.lane, provider="anthropic")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
                "WORKER_PROVIDER": "anthropic",
                "CLAW_AUTONOMOUS_MAINTENANCE": "true",
                "CLAW_AUTONOMOUS_MAINTENANCE_ENABLED": "true",
                "EVAL_ON_SELF_IMPROVE": "false",
            }

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.kairos.tick = MagicMock()
                runtime.bot.wiki.auto_research = MagicMock()
                runtime.bot.wiki.auto_scrape_sources = MagicMock()
                runtime.auto_research.run_loop = MagicMock()
                runtime.skill_registry.auto_expand = MagicMock()

                scheduler_jobs = {job.name: job for job in runtime.scheduler.list_jobs()}
                for job_name in SLOW_SCHEDULER_AGENT_JOBS:
                    self.assertIn(job_name, scheduler_jobs)
                    scheduler_jobs[job_name].handler()

                runtime.kairos.tick.assert_not_called()
                runtime.bot.wiki.auto_research.assert_not_called()
                runtime.bot.wiki.auto_scrape_sources.assert_not_called()
                runtime.auto_research.run_loop.assert_not_called()
                runtime.skill_registry.auto_expand.assert_not_called()

                for job_name, job_kind in SLOW_SCHEDULER_AGENT_JOBS.items():
                    with self.subTest(job_name=job_name):
                        rows = runtime.job_service.list(kinds=(job_kind,), limit=10)
                        self.assertEqual(len(rows), 1)
                        self.assertEqual(rows[0].status, "queued")

                runner_names = {runner.name for runner in runtime.daemon._background_job_runners}
                self.assertGreaterEqual(runner_names, set(SLOW_SCHEDULER_AGENT_JOBS))

    def test_control_roles_are_bounded_and_never_resolve_to_codex(self) -> None:
        config = AppConfig.from_env()

        for role in CONTROL_ROLES:
            with self.subTest(role=role):
                self.assertNotEqual(config.provider_for_role(role), "codex")
                self.assertLessEqual(config.timeout_for_role(role), 30.0)
                with self.assertRaises(ProviderRolePolicyError):
                    config.validate_provider_role_policy(role, "codex", timeout=30.0)
                with self.assertRaises(ProviderRolePolicyError):
                    config.validate_provider_role_policy(role, "anthropic", timeout=30.001)

    def test_control_router_calls_pass_explicit_bounded_timeouts(self) -> None:
        callsites = _control_router_ask_calls()
        self.assertGreaterEqual(len(callsites), 1)

        for callsite in callsites:
            with self.subTest(path=callsite.path.name, line=callsite.line, role=callsite.role):
                self.assertIsNotNone(callsite.timeout, "control role call must pass timeout=")
                self.assertTrue(
                    _is_bounded_control_timeout(callsite.timeout, callsite.role),
                    "control role timeout must be <= 30s or use router_timeout_for_role(..., default<=30)",
                )
                self.assertFalse(
                    _keyword_is_constant(callsite.provider, "codex"),
                    "control role call-sites must not hard-code provider='codex'",
                )

    def test_generated_codeskills_are_pending_review_and_not_executable_by_default(self) -> None:
        policy = CodeSkillGovernancePolicy()
        decision = policy.check_generated_skill(
            name="safe_skill",
            description="safe utility",
            function_name="run",
            code="def run(**kwargs):\n    return {'result': 1}\n",
            tags=[],
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.resulting_status, "pending_review")

        skill = Skill(
            name="safe_skill",
            description="safe utility",
            source_file="safe_skill.py",
            function_name="run",
            created="2026-06-05T00:00:00Z",
            status=decision.resulting_status or "",
        )
        execute_decision = policy.check_execute(skill=skill)
        self.assertFalse(execute_decision.allowed)
        self.assertEqual(execute_decision.reason, "skill_status_pending_review_not_executable")

    def test_property_graph_materialize_is_not_registered_as_a_scheduled_full_scan(self) -> None:
        offenders: list[str] = []
        for path in (REPO_ROOT / "claw_v2").glob("*.py"):
            if path.name == "property_graph.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not _is_call_named(node, "ScheduledJob"):
                    continue
                handler = _keyword_value(node, "handler")
                if handler is not None and _node_mentions(handler, {"PropertyGraphProjection", "materialize"}):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [])

    def test_startup_context_report_serializes_prompt_manifest_field(self) -> None:
        payload = StartupContextReport(root="/tmp/workspace", channel="cli").to_dict()

        self.assertIn("prompt_manifest", payload)
        self.assertIsNone(payload["prompt_manifest"])


class _ControlCallsite:
    def __init__(
        self,
        *,
        path: Path,
        line: int,
        role: str,
        timeout: ast.AST | None,
        provider: ast.AST | None,
    ) -> None:
        self.path = path
        self.line = line
        self.role = role
        self.timeout = timeout
        self.provider = provider


def _control_router_ask_calls() -> list[_ControlCallsite]:
    callsites: list[_ControlCallsite] = []
    for path in (REPO_ROOT / "claw_v2").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "ask":
                continue
            role_node = _keyword_value(node, "role")
            role = _literal_string(role_node)
            if role not in CONTROL_ROLES:
                continue
            callsites.append(
                _ControlCallsite(
                    path=path,
                    line=node.lineno,
                    role=role,
                    timeout=_keyword_value(node, "timeout"),
                    provider=_keyword_value(node, "provider"),
                )
            )
    return callsites


def _is_bounded_control_timeout(node: ast.AST | None, role: str) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value) <= 30.0
    if isinstance(node, ast.Call):
        if _call_name(node) == "router_timeout_for_role":
            role_arg = _literal_string(node.args[1]) if len(node.args) > 1 else None
            default = _keyword_value(node, "default")
            return role_arg == role and _numeric_literal(default) <= 30.0
        if isinstance(node.func, ast.Attribute) and node.func.attr == "timeout_for_role":
            return _literal_string(node.args[0] if node.args else None) == role
    return False


def _keyword_value(node: ast.Call, name: str) -> ast.AST | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _numeric_literal(node: ast.AST | None) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    return float("inf")


def _keyword_is_constant(node: ast.AST | None, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def _is_call_named(node: ast.AST, name: str) -> bool:
    if not isinstance(node, ast.Call):
        return False
    return _call_name(node) == name


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _node_mentions(node: ast.AST, names: set[str]) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in names:
            return True
        if isinstance(child, ast.Attribute) and child.attr in names:
            return True
    return False


if __name__ == "__main__":
    unittest.main()
