from __future__ import annotations

import ast
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.adapters.base import LLMRequest, LLMResponse
from claw_v2.config import AppConfig, ProviderRolePolicyError
from claw_v2.main import build_runtime, _is_git_repo, _sanitize_job_name
from claw_v2.scheduled_background_jobs import (
    A2A_PROCESS_INBOX_JOB_KIND,
    AUTO_DREAM_JOB_KIND,
    KAIROS_TICK_JOB_KIND,
    LEARNING_CONSOLIDATE_JOB_KIND,
    LEARNING_SOUL_SUGGESTIONS_JOB_KIND,
    PERF_OPTIMIZER_JOB_KIND,
    PIPELINE_POLL_JOB_KIND,
    PIPELINE_POLL_MERGES_JOB_KIND,
    SELF_IMPROVE_JOB_KIND,
    SELF_IMPROVE_RESUME_KEY,
    SUB_AGENT_JOB_KIND,
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
    "self_improve": SELF_IMPROVE_JOB_KIND,
    "pipeline_poll": PIPELINE_POLL_JOB_KIND,
    "pipeline_poll_merges": PIPELINE_POLL_MERGES_JOB_KIND,
    "a2a_process_inbox": A2A_PROCESS_INBOX_JOB_KIND,
    "auto_dream": AUTO_DREAM_JOB_KIND,
    "learning_consolidate": LEARNING_CONSOLIDATE_JOB_KIND,
    "learning_soul_suggestions": LEARNING_SOUL_SUGGESTIONS_JOB_KIND,
}

# Jobs that still run heavy (provider/subprocess/codegen) work inline in
# ``daemon.tick``. This deny-by-default exception list may only SHRINK. The
# off-tick migration train emptied it: PR 1B-c (self_improve + pipeline_poll),
# PR 1B-d (a2a + scheduled sub-agents), and the final leg (auto_dream +
# learning_consolidate + learning_soul_suggestions). It is now empty — Core
# Invariant 1 is fully closed, and the backstop below fails if ANY scheduler
# job (including a newly-added one) runs heavy work inline in daemon.tick.
_PENDING_INLINE_MIGRATION: frozenset[str] = frozenset()


class _HeavyInlineCall(BaseException):
    """Sentinel raised by a patched heavy chokepoint (provider LLM, self-improve
    loop, sub-agent dispatch, or any subprocess) when a scheduler handler invokes
    it inline. Subclasses BaseException so daemon-style ``except Exception`` guards
    do not swallow it."""


class ArchitectureInvariantTests(unittest.TestCase):
    def test_runtime_builder_and_git_probe_remain_sync(self) -> None:
        self.assertFalse(inspect.iscoroutinefunction(build_runtime))
        self.assertFalse(inspect.iscoroutinefunction(_is_git_repo))

    def test_self_improve_promotion_actions_have_critical_floor(self) -> None:
        from claw_v2.brain import _risk_floor_for_action

        self.assertEqual(_risk_floor_for_action("promote"), "critical")
        self.assertEqual(_risk_floor_for_action("promote_self-improve"), "critical")
        self.assertEqual(_risk_floor_for_action("self_improve"), "critical")

    def test_branch_promotion_executor_does_not_accept_live_head_state_flag(self) -> None:
        from claw_v2.agents import GitBranchPromotionExecutor

        source = inspect.getsource(GitBranchPromotionExecutor.__call__)
        self.assertNotIn("allow_live_head_promotion", source)
        self.assertIn("_commit_to_isolated_branch", source)

    def test_no_default_on_scheduler_job_runs_heavy_work_inline_in_daemon_tick(self) -> None:
        """Deny-by-default backstop for Core Invariant 1.

        Builds the runtime at PRODUCTION DEFAULT (no EVAL_ON_SELF_IMPROVE
        override) and sweeps EVERY registered scheduler job, invoking each
        handler under sentinels on the heavy chokepoints (provider LLM via
        ``router.ask``, the self-improve experiment loop, sub-agent dispatch,
        and any subprocess). A job that trips a sentinel ran heavy work inline
        in ``daemon.tick`` (``tick -> run_due -> job.handler()``). The only
        permitted offenders are those explicitly documented in
        ``_PENDING_INLINE_MIGRATION``; anything else — including a newly added
        inline job — fails the test. This replaces the previous positive
        5-job allowlist, which could not catch unlisted offenders and masked
        self_improve by forcing EVAL_ON_SELF_IMPROVE=false.
        """

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
                # Production default: self_improve IS enabled (not suppressed).
                "EVAL_ON_SELF_IMPROVE": "true",
            }

            with patch.dict(os.environ, env, clear=False):
                # Keep the "pytest" capability healthy so the self_improve skip
                # gate does not fire; reset after build to drop the healthcheck
                # call before the sweep. recompute_confidence is a slow *local*
                # wiki maintenance pass (no provider/subprocess/codegen) — no-op
                # it so the sweep is not dominated by ~17s of unrelated work.
                with patch("claw_v2.main._resolve_pytest_command") as mock_resolve, patch(
                    "claw_v2.wiki.WikiService.recompute_confidence", return_value=None
                ):
                    mock_resolve.return_value = (["true"], "true")
                    runtime = build_runtime(anthropic_executor=fake_anthropic)
                    mock_resolve.reset_mock()

                    # Heavy chokepoints raise a BaseException sentinel so each
                    # handler short-circuits at its FIRST heavy call. _HeavyInlineCall
                    # subclasses BaseException, so _wrap_job_handler's
                    # ``except Exception`` does not swallow it and it reaches us.
                    heavy: set[str] = set()
                    for job in runtime.scheduler.list_jobs():
                        with patch.object(
                            runtime.router, "ask", side_effect=_HeavyInlineCall
                        ), patch.object(
                            runtime.auto_research, "run_loop", side_effect=_HeavyInlineCall
                        ), patch.object(
                            runtime.sub_agents, "run_skill", side_effect=_HeavyInlineCall
                        ), patch("subprocess.run", side_effect=_HeavyInlineCall):
                            try:
                                job.handler()
                            except _HeavyInlineCall:
                                heavy.add(job.name)
                            except Exception:  # noqa: BLE001 - swallow like the daemon does
                                pass

                    offenders = heavy - _PENDING_INLINE_MIGRATION
                    self.assertEqual(
                        offenders,
                        set(),
                        "scheduler jobs run heavy work inline in daemon.tick and are not "
                        f"documented as pending migration: {sorted(offenders)}",
                    )
                    for migrated in ("self_improve", "pipeline_poll", "pipeline_poll_merges"):
                        self.assertNotIn(
                            migrated,
                            heavy,
                            f"{migrated} must be migrated off-tick and not run heavy work inline",
                        )

                    # Positive side: every known slow job enqueues a durable
                    # job and is wired as an off-tick background runner.
                    scheduler_jobs = {job.name: job for job in runtime.scheduler.list_jobs()}
                    runner_names = {runner.name for runner in runtime.daemon._background_job_runners}
                    for job_name, job_kind in SLOW_SCHEDULER_AGENT_JOBS.items():
                        with self.subTest(job_name=job_name):
                            self.assertIn(job_name, scheduler_jobs)
                            self.assertIn(job_name, runner_names)
                            rows = runtime.job_service.list(kinds=(job_kind,), limit=10)
                            self.assertEqual(len(rows), 1)
                            self.assertEqual(rows[0].status, "queued")

    def test_self_improve_is_migrated_off_tick_and_does_not_run_inline(self) -> None:
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
                # Production default: self_improve is enabled. The previous backstop
                # forced EVAL_ON_SELF_IMPROVE=false, which hid the inline violation.
                "EVAL_ON_SELF_IMPROVE": "true",
            }

            with patch.dict(os.environ, env, clear=False):
                # _resolve_pytest_command is reached by the startup healthcheck
                # AND by the *inline* self_improve handler. Returning a non-None
                # pytest_path keeps the "pytest" capability healthy so the skip
                # gate does not fire; reset_mock() after build isolates the
                # handler's own call from the build-time healthcheck call.
                with patch("claw_v2.main._resolve_pytest_command") as mock_resolve:
                    mock_resolve.return_value = (["true"], "true")
                    runtime = build_runtime(anthropic_executor=fake_anthropic)
                    runtime.auto_research.run_loop = MagicMock()
                    mock_resolve.reset_mock()

                    jobs = {job.name: job for job in runtime.scheduler.list_jobs()}
                    self.assertIn(
                        "self_improve",
                        jobs,
                        "self_improve must be registered at production default (EVAL_ON_SELF_IMPROVE=true)",
                    )

                    jobs["self_improve"].handler()

                    # The scheduler/control path must not run pytest or the
                    # Codex-aware experiment loop inline.
                    mock_resolve.assert_not_called()
                    runtime.auto_research.run_loop.assert_not_called()

                    # Heavy work must be wired as an off-tick durable runner...
                    runner_names = {runner.name for runner in runtime.daemon._background_job_runners}
                    self.assertIn("self_improve", runner_names)

                    # ...and the scheduler handler must enqueue a durable job.
                    rows = runtime.job_service.list(kinds=(SELF_IMPROVE_JOB_KIND,), limit=10)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0].status, "queued")

    def test_self_improve_runner_does_not_drain_queued_jobs_when_disabled(self) -> None:
        """The EVAL_ON_SELF_IMPROVE kill-switch must apply to the durable runner,
        not only the enqueue side: when disabled, an already-queued
        scheduler.self_improve row (enqueued before the flag flipped, or a retry)
        must remain unclaimed and no pytest/Codex/git work may run. Matches the
        old inline behavior of simply not running self-improve when off."""

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
                runtime.auto_research.run_loop = MagicMock()

                # A durable job left over from when the flag was on.
                runtime.job_service.enqueue(
                    kind=SELF_IMPROVE_JOB_KIND,
                    payload={},
                    resume_key=SELF_IMPROVE_RESUME_KEY,
                    metadata={"source": "test"},
                )

                runners = {runner.name: runner for runner in runtime.daemon._background_job_runners}
                self.assertIn("self_improve", runners)
                with patch("subprocess.run", side_effect=AssertionError("self-improve must not run when disabled")):
                    runners["self_improve"].handler()

                rows = runtime.job_service.list(kinds=(SELF_IMPROVE_JOB_KIND,), limit=10)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].status, "queued", "disabled kill-switch must leave the job unclaimed")
                runtime.auto_research.run_loop.assert_not_called()

    def test_pipeline_poll_jobs_are_migrated_off_tick_and_do_not_run_inline(self) -> None:
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
            }

            with patch.dict(os.environ, env, clear=False):
                # Patch at class level BEFORE build so the (current) raw
                # ``handler=pipeline.poll_actionable`` registration captures the
                # sentinel; the migrated handler must never invoke them inline.
                with patch("claw_v2.pipeline.PipelineService.poll_actionable") as mock_poll, patch(
                    "claw_v2.pipeline.PipelineService.poll_merges"
                ) as mock_merges:
                    runtime = build_runtime(anthropic_executor=fake_anthropic)

                    jobs = {job.name: job for job in runtime.scheduler.list_jobs()}
                    for name in ("pipeline_poll", "pipeline_poll_merges"):
                        self.assertIn(name, jobs)
                        jobs[name].handler()

                    # No git worktree / worker LLM / pytest / git push inline.
                    mock_poll.assert_not_called()
                    mock_merges.assert_not_called()

                    runner_names = {runner.name for runner in runtime.daemon._background_job_runners}
                    self.assertIn("pipeline_poll", runner_names)
                    self.assertIn("pipeline_poll_merges", runner_names)

                    for kind in (PIPELINE_POLL_JOB_KIND, PIPELINE_POLL_MERGES_JOB_KIND):
                        with self.subTest(kind=kind):
                            rows = runtime.job_service.list(kinds=(kind,), limit=10)
                            self.assertEqual(len(rows), 1)
                            self.assertEqual(rows[0].status, "queued")

    def test_a2a_process_inbox_is_migrated_off_tick_and_does_not_dispatch_inline(self) -> None:
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
            }

            with patch.dict(os.environ, env, clear=False):
                # Patch at class level BEFORE build so the (current) inline
                # ``handler=a2a.process_inbox`` registration captures the
                # sentinel; the migrated handler must only enqueue.
                with patch("claw_v2.a2a.A2AService.process_inbox") as mock_inbox:
                    runtime = build_runtime(anthropic_executor=fake_anthropic)

                    jobs = {job.name: job for job in runtime.scheduler.list_jobs()}
                    self.assertIn("a2a_process_inbox", jobs)
                    jobs["a2a_process_inbox"].handler()

                    mock_inbox.assert_not_called()

                    runner_names = {runner.name for runner in runtime.daemon._background_job_runners}
                    self.assertIn("a2a_process_inbox", runner_names)

                    rows = runtime.job_service.list(kinds=(A2A_PROCESS_INBOX_JOB_KIND,), limit=10)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0].status, "queued")

    def test_scheduled_sub_agent_jobs_are_migrated_off_tick_and_do_not_dispatch_inline(self) -> None:
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
            }

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                # run_skill is the provider-dispatch chokepoint; the scheduler
                # handler must enqueue instead of calling it inline.
                runtime.sub_agents.run_skill = MagicMock()

                sub_agent_names = {
                    f"{_sanitize_job_name(j.agent)}_{_sanitize_job_name(j.skill)}"
                    for j in runtime.config.scheduled_sub_agents
                }
                self.assertTrue(sub_agent_names, "default config must register scheduled sub-agent jobs")

                jobs = {job.name: job for job in runtime.scheduler.list_jobs()}
                for name in sub_agent_names:
                    self.assertIn(name, jobs)
                    jobs[name].handler()

                runtime.sub_agents.run_skill.assert_not_called()

                # All scheduled sub-agents share one off-tick runner...
                runner_names = {runner.name for runner in runtime.daemon._background_job_runners}
                self.assertIn("sub_agent", runner_names)

                # ...and each enqueues its own durable job (deduped per agent/skill).
                rows = runtime.job_service.list(kinds=(SUB_AGENT_JOB_KIND,), limit=50)
                self.assertEqual(len(rows), len(sub_agent_names))
                for row in rows:
                    self.assertEqual(row.status, "queued")

    def test_control_roles_are_bounded_and_never_resolve_to_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "HOME": str(Path.home()),
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
            }
            with patch.dict(os.environ, env, clear=True):
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
        for path in _package_python_files():
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
    for path in _package_python_files():
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


def _package_python_files() -> list[Path]:
    return [
        path
        for path in (REPO_ROOT / "claw_v2").rglob("*.py")
        if "__pycache__" not in path.parts
    ]


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
