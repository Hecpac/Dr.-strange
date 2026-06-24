from __future__ import annotations

import ast
import inspect
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.adapters.base import LLMRequest, LLMResponse
from claw_v2.config import AppConfig, ProviderRolePolicyError
from claw_v2.main import build_runtime, _is_git_repo, _sanitize_job_name
from claw_v2.scheduled_background_jobs import (
    A2A_PROCESS_INBOX_JOB_KIND,
    APPROVAL_SWEEP_JOB_KIND,
    AUTO_DREAM_JOB_KIND,
    DAEMON_HEALTH_CHECK_JOB_KIND,
    KAIROS_TICK_JOB_KIND,
    LEARNING_CONSOLIDATE_JOB_KIND,
    LEARNING_SOUL_SUGGESTIONS_JOB_KIND,
    PERF_OPTIMIZER_JOB_KIND,
    PIPELINE_POLL_JOB_KIND,
    PIPELINE_POLL_MERGES_JOB_KIND,
    SELF_IMPROVE_JOB_KIND,
    SELF_IMPROVE_RESUME_KEY,
    SITE_MONITOR_JOB_KIND,
    SUB_AGENT_JOB_KIND,
    WIKI_RESEARCH_JOB_KIND,
    WIKI_SCRAPE_JOB_KIND,
)
from claw_v2.skill_expand_jobs import SKILL_EXPAND_JOB_KIND
from claw_v2.skills import CodeSkillGovernancePolicy, Skill
from claw_v2.task_handler import TaskHandler
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
    "approval_sweep": APPROVAL_SWEEP_JOB_KIND,
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


# F1.1b read-lock discipline (RAÍZ #1) -----------------------------------------
# Every SQL execution on a RuntimeDb-backed store's shared connection
# (``self._conn`` — the RuntimeDb connection handle) must hold the shared lock,
# so the single connection never sees concurrent access. "Holds the lock" =
# lexically inside ``with self._lock:`` or a ``self._db.<cursor|transaction|
# try_cursor|try_acquire>()`` block, or in an ``@_synchronized`` method.
_POLICED_CONN_SQL_ATTRS = frozenset(
    {"execute", "executescript", "executemany", "cursor", "commit", "rollback"}
)
_RUNTIMEDB_LOCK_CTX_ATTRS = frozenset({"cursor", "transaction", "try_cursor", "try_acquire"})


def _is_runtime_conn(node: ast.AST) -> bool:
    """True for ``self._conn`` (the RuntimeDb connection handle) or
    ``self._db._conn`` (reaching past the handle to the raw connection — the
    handle exposes no public cursor, so this is the tempting bypass).

    SYNTACTIC match only. A connection bound to a local alias
    (``c = self._conn; c.execute(...)``) or a cursor captured under the lock and
    iterated/fetched outside it are out of this detector's scope; those are
    covered dynamically by
    tests/test_sqlite_runtime.py::RuntimeDbConcurrencyTests. No store uses
    either pattern today (verified in the F1.1b audit)."""
    if not (isinstance(node, ast.Attribute) and node.attr == "_conn"):
        return False
    base = node.value
    if isinstance(base, ast.Name) and base.id == "self":
        return True  # self._conn
    return (
        isinstance(base, ast.Attribute)
        and base.attr == "_db"
        and isinstance(base.value, ast.Name)
        and base.value.id == "self"
    )  # self._db._conn


def _with_holds_store_lock(node: ast.With | ast.AsyncWith) -> bool:
    """True if a ``with`` acquires the store's serialization lock:
    ``with self._lock:`` or ``with self._db.<cursor|transaction|try_cursor|
    try_acquire>() [as ...]:`` (all hold the shared RuntimeDb lock)."""
    for item in node.items:
        ctx = item.context_expr
        if (
            isinstance(ctx, ast.Attribute)
            and ctx.attr == "_lock"
            and isinstance(ctx.value, ast.Name)
            and ctx.value.id == "self"
        ):
            return True
        if (
            isinstance(ctx, ast.Call)
            and isinstance(ctx.func, ast.Attribute)
            and ctx.func.attr in _RUNTIMEDB_LOCK_CTX_ATTRS
            and isinstance(ctx.func.value, ast.Attribute)
            and ctx.func.value.attr == "_db"
            and isinstance(ctx.func.value.value, ast.Name)
            and ctx.func.value.value.id == "self"
        ):
            return True
    return False


def _method_holds_lock_via_decorator(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """True if the method is decorated ``@_synchronized`` (acquires self._lock)."""
    for dec in func.decorator_list:
        name = dec.attr if isinstance(dec, ast.Attribute) else getattr(dec, "id", None)
        if name == "_synchronized":
            return True
    return False


def _bare_conn_sql_offenders(source: str, *, exempt_methods: set[str]) -> list[str]:
    """Return ``Class.method:line`` for every ``self._conn`` / ``self._db._conn``
    SQL call executed WITHOUT the store's serialization lock held — not lexically
    inside ``with self._lock:`` / ``self._db.<ctx>()``, not in an
    ``@_synchronized`` method, not in ``exempt_methods``. SQL on any other object
    (e.g. a dedicated local connection for ``maintenance_vacuum``) is not policed.

    Exemptions are keyed by ``Class.method`` (NOT bare method name), so a new
    class reusing a generic allowlisted name (``__init__``, ``ensure_schema``,
    ``_table_exists``) does not silently inherit another class's exemption."""
    tree = ast.parse(source)
    offenders: list[str] = []

    def process(node: ast.AST, *, lock_held: bool, method: str) -> None:
        # Nested function defs have their own (separate) lock scope.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return
        if isinstance(node, (ast.With, ast.AsyncWith)):
            held = lock_held or _with_holds_store_lock(node)
            for item in node.items:
                process(item.context_expr, lock_held=lock_held, method=method)
            for stmt in node.body:
                process(stmt, lock_held=held, method=method)
            return
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _POLICED_CONN_SQL_ATTRS
            and _is_runtime_conn(node.func.value)
            and not lock_held
            and method not in exempt_methods
        ):
            offenders.append(f"{method}:{node.lineno}")
        for child in ast.iter_child_nodes(node):
            process(child, lock_held=lock_held, method=method)

    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        for member in cls.body:
            if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _method_holds_lock_via_decorator(member):
                continue
            qualname = f"{cls.name}.{member.name}"
            for stmt in member.body:
                process(stmt, lock_held=False, method=qualname)
    return offenders


class _HeavyInlineCall(BaseException):
    """Sentinel raised by a patched heavy chokepoint (provider LLM, self-improve
    loop, sub-agent dispatch, or any subprocess) when a scheduler handler invokes
    it inline. Subclasses BaseException so daemon-style ``except Exception`` guards
    do not swallow it."""


class ArchitectureInvariantTests(unittest.TestCase):
    def test_runtime_builder_and_git_probe_remain_sync(self) -> None:
        self.assertFalse(inspect.iscoroutinefunction(build_runtime))
        self.assertFalse(inspect.iscoroutinefunction(_is_git_repo))

    def test_liveness_signal_has_a_consumer(self) -> None:
        """F0.3 tripwire: the daemon liveness signal lives in a shared atomic
        JSON sink (``claw_v2/liveness.py``). The WRITER (lifecycle) and the
        READER (diagnostics) must both reference that shared module so they
        cannot drift to different paths and silently lose the signal."""
        writer = (REPO_ROOT / "claw_v2" / "lifecycle.py").read_text(encoding="utf-8")
        reader = (REPO_ROOT / "claw_v2" / "diagnostics.py").read_text(encoding="utf-8")
        self.assertIn("liveness.write_liveness", writer)
        self.assertIn("liveness.liveness_sink_path", writer)
        self.assertIn("liveness.read_liveness", reader)
        self.assertIn("liveness.liveness_sink_path", reader)

    def test_operational_state_writers_are_atomic(self) -> None:
        """F0.4 tripwire: operational state files — the shared task board
        (``task_board.py``) and the observation-window circuit/budget freeze
        state (``observation_window.py``) — must be persisted atomically
        (temp file → fsync → ``os.replace`` → parent-dir fsync), never via a
        direct non-atomic ``Path.write_text``. A torn write here drops a task
        result or fails the next boot's circuit/budget restore. Only writes are
        policed (``read_text`` is fine). A new operational state module belongs
        in this allowlist with its write routed through the established atomic
        helper — not exempted from it."""
        operational_state_modules = (
            "claw_v2/task_board.py",
            "claw_v2/observation_window.py",
        )
        offenders: list[str] = []
        for rel_path in operational_state_modules:
            tree = ast.parse((REPO_ROOT / rel_path).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "write_text"
                ):
                    offenders.append(f"{rel_path}:{node.lineno}")
        self.assertEqual(
            offenders,
            [],
            f"operational state writer uses non-atomic write_text: {offenders}",
        )

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

    def test_branch_promotion_executor_runs_diff_scoped_tooling_gate(self) -> None:
        from claw_v2.agents import GitBranchPromotionExecutor

        source = inspect.getsource(GitBranchPromotionExecutor.__call__)
        self.assertIn("tooling_gate.evaluate", source)
        self.assertIn("PromotionToolingError", source)

    def test_promotion_sensitive_path_denylist_covers_runtime_chokepoints(self) -> None:
        from claw_v2.agents import PROMOTION_SENSITIVE_PATH_PATTERNS

        required = {
            "claw_v2/brain.py",
            "claw_v2/agents.py",
            "claw_v2/approval.py",
            "claw_v2/approval_gate.py",
            "claw_v2/config.py",
            "claw_v2/main.py",
            "claw_v2/tools.py",
            "claw_v2/scheduler*",
            "claw_v2/scheduled_background_jobs.py",
            "claw_v2/computer.py",
            "claw_v2/memory*",
            "claw_v2/secrets*",
            "claw_v2/auth*",
            "claw_v2/subprocess_runner.py",
            "tests/test_architecture_invariants.py",
            "claw_v2/INTERNAL_WIRING.md",
            "CLAUDE.md",
            "AGENTS.md",
        }
        self.assertTrue(required.issubset(set(PROMOTION_SENSITIVE_PATH_PATTERNS)))

    def test_recovery_job_drainer_stays_wired_into_runtime(self) -> None:
        # 2026-06-10 audit C1: recovery_jobs accumulated forever because
        # resolve_recovery_job had no runtime caller (a false promise of
        # continuity). The off-tick RecoveryJobDrainRunner must stay registered
        # in main.py — losing the wiring regresses it back to a cemetery.
        main_source = (REPO_ROOT / "claw_v2" / "main.py").read_text(encoding="utf-8")
        self.assertIn("RecoveryJobDrainRunner", main_source)
        self.assertIn('name="recovery_drain"', main_source)

    def test_task_handler_lifts_contract_artifact_before_promote_gate(self) -> None:
        source = inspect.getsource(TaskHandler._run_autonomous_task)
        coordinator_source = (REPO_ROOT / "claw_v2" / "coordinator.py").read_text(encoding="utf-8")
        runner_source = (REPO_ROOT / "claw_v2" / "verification" / "local_tool_runner.py").read_text(
            encoding="utf-8"
        )
        tools_source = (REPO_ROOT / "claw_v2" / "tools.py").read_text(encoding="utf-8")
        main_source = (REPO_ROOT / "claw_v2" / "main.py").read_text(encoding="utf-8")
        consume_idx = source.find("consume_current_tool_contract_results")
        lift_idx = source.find("lift_artifacts_to_checkpoint")
        gate_idx = source.find("apply_promote_gate_to_checkpoint")

        self.assertGreaterEqual(consume_idx, 0)
        self.assertGreaterEqual(lift_idx, 0)
        self.assertGreaterEqual(gate_idx, 0)
        self.assertLess(consume_idx, lift_idx)
        self.assertLess(consume_idx, gate_idx)
        self.assertLess(lift_idx, gate_idx)
        self.assertIn(
            "reset_current_tool_contract_results(session_id=session_id, scope_id=task_id)",
            source,
        )
        self.assertIn(
            "consume_current_tool_contract_results(",
            source,
        )
        self.assertIn("scope_id=task_id", source)
        self.assertIn("contract_artifact_scope(task_id)", source)
        self.assertIn("contract_artifact_scope(worker_contract_scope)", coordinator_source)
        self.assertNotIn("defensive optional verification import", coordinator_source)
        self.assertNotIn("contract_artifact_scope = None", coordinator_source)
        self.assertIn(
            "remember_tool_contract_result(",
            tools_source,
        )
        self.assertIn("scope_id=contract_scope_id", tools_source)
        self.assertIn("contract_scope_id=current_contract_artifact_scope()", main_source)
        self.assertIn("_SCOPE_CONTRACT_TOOL_RESULTS: dict[str, list", runner_source)
        self.assertIn("setdefault(effective_scope_id, []).append", runner_source)
        self.assertIn("verification_status=verification_status", source)
        self.assertIn("last_checkpoint=completed_checkpoint", source)

    def test_computer_module_does_not_import_pyautogui_at_module_scope(self) -> None:
        tree = ast.parse((REPO_ROOT / "claw_v2" / "computer.py").read_text(encoding="utf-8"))
        offenders: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                offenders.extend(alias.name for alias in node.names if alias.name == "pyautogui")
            elif isinstance(node, ast.ImportFrom) and node.module == "pyautogui":
                offenders.append(node.module)
        self.assertEqual(offenders, [])

    def test_subprocess_run_calls_in_runtime_code_have_timeouts(self) -> None:
        offenders: list[str] = []
        for path in sorted((REPO_ROOT / "claw_v2").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_subprocess_run = (
                    isinstance(func, ast.Attribute)
                    and func.attr == "run"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "subprocess"
                )
                if not is_subprocess_run:
                    continue
                kwargs = {keyword.arg for keyword in node.keywords if keyword.arg}
                if "timeout" not in kwargs:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [])

    def test_runtime_code_does_not_introduce_async_subprocess_exec(self) -> None:
        legacy_voice_subprocesses = {
            ("claw_v2/voice.py", "_transcribe_local"),
            ("claw_v2/voice.py", "extract_audio"),
            ("claw_v2/voice.py", "_wav_to_ogg"),
            ("claw_v2/voice.py", "_mp3_to_ogg"),
        }
        offenders: list[str] = []
        for path in sorted((REPO_ROOT / "claw_v2").rglob("*.py")):
            rel_path = str(path.relative_to(REPO_ROOT))
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for function in [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            ]:
                for node in ast.walk(function):
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    is_create_subprocess_exec = (
                        isinstance(func, ast.Attribute)
                        and func.attr == "create_subprocess_exec"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "asyncio"
                    )
                    if not is_create_subprocess_exec:
                        continue
                    if (rel_path, function.name) in legacy_voice_subprocesses:
                        continue
                    offenders.append(f"{rel_path}:{node.lineno}:{function.name}")
            for node in tree.body:
                if not isinstance(node, ast.Expr | ast.Assign | ast.AnnAssign):
                    continue
                for call in ast.walk(node):
                    if not isinstance(call, ast.Call):
                        continue
                    func = call.func
                    if (
                        isinstance(func, ast.Attribute)
                        and func.attr == "create_subprocess_exec"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "asyncio"
                    ):
                        offenders.append(f"{rel_path}:{call.lineno}:module")
        self.assertEqual(offenders, [])

    def test_runtime_code_restricts_direct_subprocess_popen(self) -> None:
        allowed_popen_callers = {
            ("claw_v2/chrome.py", "_spawn_chrome"),  # long-lived managed Chrome process
            ("claw_v2/subprocess_runner.py", "run_subprocess_bounded"),
            ("claw_v2/terminal_bridge.py", "run_session"),  # long-lived PTY session runner
        }
        offenders: list[str] = []
        for path in sorted((REPO_ROOT / "claw_v2").rglob("*.py")):
            rel_path = str(path.relative_to(REPO_ROOT))
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for function in [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            ]:
                for node in ast.walk(function):
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    is_popen = (
                        isinstance(func, ast.Attribute)
                        and func.attr == "Popen"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "subprocess"
                    )
                    if not is_popen:
                        continue
                    if (rel_path, function.name) in allowed_popen_callers:
                        continue
                    offenders.append(f"{rel_path}:{node.lineno}:{function.name}")
        self.assertEqual(offenders, [])

    def test_runtime_code_does_not_use_shell_true_or_os_system(self) -> None:
        offenders: list[str] = []
        for path in sorted((REPO_ROOT / "claw_v2").rglob("*.py")):
            rel_path = str(path.relative_to(REPO_ROOT))
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_os_system = (
                    isinstance(func, ast.Attribute)
                    and func.attr == "system"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                )
                if is_os_system:
                    offenders.append(f"{rel_path}:{node.lineno}:os.system")
                for keyword in node.keywords:
                    if keyword.arg != "shell":
                        continue
                    if isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                        offenders.append(f"{rel_path}:{node.lineno}:shell=True")
        self.assertEqual(offenders, [])

    def test_vacuum_only_runs_off_tick(self) -> None:
        """F0.2c: VACUUM is blocking and needs ~2x free disk, so it must never
        be reachable from ``daemon.tick`` / ``cron.run_due``, and its only
        ``main.py`` call-site must be wired through
        ``register_background_job_runner`` (the off-tick mechanism). Mirrors
        the off-tick discipline of Core Invariant 1."""
        # 1. No VACUUM anywhere in the tick / scheduler hot path.
        for rel in ("daemon.py", "cron.py"):
            src = (REPO_ROOT / "claw_v2" / rel).read_text(encoding="utf-8")
            self.assertNotIn("vacuum", src.lower(), f"VACUUM must not appear in {rel}")

        # 2. Every main.py function that calls .maintenance_vacuum() must be
        #    registered as the handler= of a register_background_job_runner call.
        tree = ast.parse((REPO_ROOT / "claw_v2" / "main.py").read_text(encoding="utf-8"))

        def _directly_calls_maintenance_vacuum(func: ast.AST) -> bool:
            # Walk the function body WITHOUT descending into nested functions,
            # so only the call's nearest-enclosing def matches (not ancestors).
            stack = list(getattr(func, "body", []))
            while stack:
                node = stack.pop()
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "maintenance_vacuum"
                ):
                    return True
                stack.extend(ast.iter_child_nodes(node))
            return False

        vacuum_funcs = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and _directly_calls_maintenance_vacuum(node)
        }
        self.assertTrue(vacuum_funcs, "expected a main.py function calling maintenance_vacuum")

        registered_handlers: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "register_background_job_runner"
            ):
                for kw in node.keywords:
                    if kw.arg == "handler" and isinstance(kw.value, ast.Name):
                        registered_handlers.add(kw.value.id)

        unwired = vacuum_funcs - registered_handlers
        self.assertEqual(
            unwired,
            set(),
            f"maintenance_vacuum call-sites not wired off-tick: {unwired}",
        )

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
            return LLMResponse(
                content="<response>ok</response>", lane=req.lane, provider="anthropic"
            )

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
                with (
                    patch("claw_v2.main._resolve_pytest_command") as mock_resolve,
                    patch("claw_v2.wiki.WikiService.recompute_confidence", return_value=None),
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
                        with (
                            patch.object(runtime.router, "ask", side_effect=_HeavyInlineCall),
                            patch.object(
                                runtime.auto_research, "run_loop", side_effect=_HeavyInlineCall
                            ),
                            patch.object(
                                runtime.sub_agents, "run_skill", side_effect=_HeavyInlineCall
                            ),
                            patch("subprocess.run", side_effect=_HeavyInlineCall),
                        ):
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
                    runner_names = {
                        runner.name for runner in runtime.daemon._background_job_runners
                    }
                    for job_name, job_kind in SLOW_SCHEDULER_AGENT_JOBS.items():
                        with self.subTest(job_name=job_name):
                            self.assertIn(job_name, scheduler_jobs)
                            self.assertIn(job_name, runner_names)
                            rows = runtime.job_service.list(kinds=(job_kind,), limit=10)
                            self.assertEqual(len(rows), 1)
                            self.assertEqual(rows[0].status, "queued")

    def test_self_improve_is_migrated_off_tick_and_does_not_run_inline(self) -> None:
        def fake_anthropic(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="<response>ok</response>", lane=req.lane, provider="anthropic"
            )

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
                    runner_names = {
                        runner.name for runner in runtime.daemon._background_job_runners
                    }
                    self.assertIn("self_improve", runner_names)

                    # ...and the scheduler handler must enqueue a durable job.
                    rows = runtime.job_service.list(kinds=(SELF_IMPROVE_JOB_KIND,), limit=10)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0].status, "queued")

    def test_daemon_health_check_is_off_tick_and_fires_within_window(self) -> None:
        """AH5 (2026-06-11): the 20:58 health check ran kairos.run_health_check
        (LLM judge, 30s timeout) inline in daemon.tick, and escaped the sweep
        because it was registered in lifecycle.run() behind an exact-minute
        match. Now the guard is registered in build_runtime, only enqueues a
        durable job, and uses a window so a slow tick cannot skip the day."""

        def fake_anthropic(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="<response>ok</response>", lane=req.lane, provider="anthropic"
            )

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
            }

            with patch.dict(os.environ, env, clear=False):
                with patch("claw_v2.main._resolve_pytest_command") as mock_resolve:
                    mock_resolve.return_value = (["true"], "true")
                    runtime = build_runtime(anthropic_executor=fake_anthropic)

                jobs = {job.name: job for job in runtime.scheduler.list_jobs()}
                self.assertIn("daemon_health_check_guard", jobs)

                # Drive the guard inside the 20:58 window with the heavy
                # chokepoints sentinelled: it must only enqueue.
                from datetime import datetime as real_datetime

                due = real_datetime(2026, 6, 11, 20, 59, 30)
                with patch("claw_v2.main.datetime") as mock_dt:
                    mock_dt.now.return_value = due
                    with (
                        patch.object(runtime.router, "ask", side_effect=_HeavyInlineCall),
                        patch.object(
                            runtime.kairos, "run_health_check", side_effect=_HeavyInlineCall
                        ),
                        patch("subprocess.run", side_effect=_HeavyInlineCall),
                    ):
                        jobs["daemon_health_check_guard"].handler()

                    rows = runtime.job_service.list(kinds=(DAEMON_HEALTH_CHECK_JOB_KIND,), limit=10)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0].status, "queued")

                    # At-most-once per day: a second tick in the same window
                    # must not enqueue again.
                    jobs["daemon_health_check_guard"].handler()

                # The judge itself is wired as an off-tick durable runner.
                runner_names = {runner.name for runner in runtime.daemon._background_job_runners}
                self.assertIn("daemon_health_check", runner_names)

    def test_daemon_health_check_window_tolerates_slow_ticks(self) -> None:
        from datetime import datetime as real_datetime

        from claw_v2.scheduled_background_jobs import daemon_health_check_due

        # A tick landing minutes late (the old `minute != 58` exact match
        # skipped these) still fires within the window.
        late = real_datetime(2026, 6, 11, 21, 3, 12)
        self.assertEqual(daemon_health_check_due(late, ""), "2026-06-11")
        # Outside the window: no fire.
        too_late = real_datetime(2026, 6, 11, 21, 20, 0)
        self.assertIsNone(daemon_health_check_due(too_late, ""))
        before = real_datetime(2026, 6, 11, 20, 57, 59)
        self.assertIsNone(daemon_health_check_due(before, ""))
        # Same day key: at-most-once.
        self.assertIsNone(daemon_health_check_due(late, "2026-06-11"))

    def test_site_monitor_probe_is_off_tick(self) -> None:
        """AM-SITEMON (2026-06-12): the HTTP probe (httpx, 15s timeout) used
        to run inline in the scheduler handler — network I/O inside
        daemon.tick. The handler must only enqueue a durable job; the probe
        runs in a daemon background runner."""

        def fake_anthropic(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="<response>ok</response>", lane=req.lane, provider="anthropic"
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime_config = root / "runtime.yml"
            runtime_config.write_text(
                "monitored_sites:\n"
                "  - name: status page\n"
                "    url: https://status.example.com\n"
                "    interval_seconds: 900\n",
                encoding="utf-8",
            )
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
                "RUNTIME_CONFIG_PATH": str(runtime_config),
            }
            with patch.dict(os.environ, env, clear=False):
                with patch("claw_v2.main._resolve_pytest_command") as mock_resolve:
                    mock_resolve.return_value = (["true"], "true")
                    runtime = build_runtime(anthropic_executor=fake_anthropic)

                jobs = {job.name: job for job in runtime.scheduler.list_jobs()}
                job_name = next(name for name in jobs if name.startswith("site_monitor_"))

                # The scheduler handler must not touch the network.
                import httpx

                with patch.object(httpx, "get", side_effect=_HeavyInlineCall):
                    jobs[job_name].handler()

                rows = runtime.job_service.list(kinds=(SITE_MONITOR_JOB_KIND,), limit=10)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].status, "queued")
                self.assertEqual(rows[0].payload["url"], "https://status.example.com")

                runner_names = {runner.name for runner in runtime.daemon._background_job_runners}
                self.assertIn("site_monitor", runner_names)

    def test_daemon_tick_heartbeat_snapshot_is_throttled(self) -> None:
        """AM-HB (2026-06-12): heartbeat.collect() scans approvals, runs cost
        SQL and reads every agent state file — it must not run on every 60s
        tick, only at the snapshot interval."""
        from claw_v2.daemon import ClawDaemon

        class _Scheduler:
            def run_due(self, now=None):
                return []

        class _Heartbeat:
            def __init__(self) -> None:
                self.collect_calls = 0

            def collect(self):
                self.collect_calls += 1
                from claw_v2.heartbeat import HeartbeatSnapshot

                return HeartbeatSnapshot(
                    timestamp="t",
                    pending_approvals=0,
                    pending_approval_ids=[],
                    agents={},
                    lane_metrics={},
                )

        heartbeat = _Heartbeat()
        daemon = ClawDaemon(
            scheduler=_Scheduler(),
            heartbeat=heartbeat,
            heartbeat_snapshot_interval=300.0,
        )

        base = 1_000_000.0
        for offset in (0.0, 60.0, 120.0, 180.0, 240.0):
            daemon.tick(now=base + offset)
        self.assertEqual(heartbeat.collect_calls, 1)
        daemon.tick(now=base + 300.0)
        self.assertEqual(heartbeat.collect_calls, 2)

    def test_self_improve_runner_does_not_drain_queued_jobs_when_disabled(self) -> None:
        """The EVAL_ON_SELF_IMPROVE kill-switch must apply to the durable runner,
        not only the enqueue side: when disabled, an already-queued
        scheduler.self_improve row (enqueued before the flag flipped, or a retry)
        must remain unclaimed and no pytest/Codex/git work may run. Matches the
        old inline behavior of simply not running self-improve when off."""

        def fake_anthropic(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="<response>ok</response>", lane=req.lane, provider="anthropic"
            )

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
                with patch(
                    "subprocess.run",
                    side_effect=AssertionError("self-improve must not run when disabled"),
                ):
                    runners["self_improve"].handler()

                rows = runtime.job_service.list(kinds=(SELF_IMPROVE_JOB_KIND,), limit=10)
                self.assertEqual(len(rows), 1)
                self.assertEqual(
                    rows[0].status, "queued", "disabled kill-switch must leave the job unclaimed"
                )
                runtime.auto_research.run_loop.assert_not_called()

    def test_pipeline_poll_jobs_are_migrated_off_tick_and_do_not_run_inline(self) -> None:
        def fake_anthropic(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="<response>ok</response>", lane=req.lane, provider="anthropic"
            )

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
                with (
                    patch("claw_v2.pipeline.PipelineService.poll_actionable") as mock_poll,
                    patch("claw_v2.pipeline.PipelineService.poll_merges") as mock_merges,
                ):
                    runtime = build_runtime(anthropic_executor=fake_anthropic)

                    jobs = {job.name: job for job in runtime.scheduler.list_jobs()}
                    for name in ("pipeline_poll", "pipeline_poll_merges"):
                        self.assertIn(name, jobs)
                        jobs[name].handler()

                    # No git worktree / worker LLM / pytest / git push inline.
                    mock_poll.assert_not_called()
                    mock_merges.assert_not_called()

                    runner_names = {
                        runner.name for runner in runtime.daemon._background_job_runners
                    }
                    self.assertIn("pipeline_poll", runner_names)
                    self.assertIn("pipeline_poll_merges", runner_names)

                    for kind in (PIPELINE_POLL_JOB_KIND, PIPELINE_POLL_MERGES_JOB_KIND):
                        with self.subTest(kind=kind):
                            rows = runtime.job_service.list(kinds=(kind,), limit=10)
                            self.assertEqual(len(rows), 1)
                            self.assertEqual(rows[0].status, "queued")

    def test_a2a_process_inbox_is_migrated_off_tick_and_does_not_dispatch_inline(self) -> None:
        def fake_anthropic(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="<response>ok</response>", lane=req.lane, provider="anthropic"
            )

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

                    runner_names = {
                        runner.name for runner in runtime.daemon._background_job_runners
                    }
                    self.assertIn("a2a_process_inbox", runner_names)

                    rows = runtime.job_service.list(kinds=(A2A_PROCESS_INBOX_JOB_KIND,), limit=10)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0].status, "queued")

    def test_scheduled_sub_agent_jobs_are_migrated_off_tick_and_do_not_dispatch_inline(
        self,
    ) -> None:
        def fake_anthropic(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="<response>ok</response>", lane=req.lane, provider="anthropic"
            )

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
                self.assertTrue(
                    sub_agent_names, "default config must register scheduled sub-agent jobs"
                )

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
                if handler is not None and _node_mentions(
                    handler, {"PropertyGraphProjection", "materialize"}
                ):
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
        path for path in (REPO_ROOT / "claw_v2").rglob("*.py") if "__pycache__" not in path.parts
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


class RuntimeDbReadLockDisciplineTests(unittest.TestCase):
    """F1.1b: enforce that the single-writer read-lock discipline (every SQL on
    the shared ``self._conn`` runs under the shared lock) cannot silently
    regress, and that the detector backing the tripwire actually has teeth."""

    def test_bare_conn_detector_has_teeth(self) -> None:
        """The detector must flag an unguarded ``self._conn`` / ``self._db._conn``
        SQL call, clear it when the call is under ``with self._lock:`` /
        ``self._db.try_acquire()`` or in an ``@_synchronized`` method, and honor
        a ``Class.method`` allowlist (NOT a bare method name). Without teeth the
        tripwire below would be a no-op."""
        source = textwrap.dedent(
            """
            class S:
                def read_bad(self):
                    return self._conn.execute("SELECT 1").fetchone()

                def reach_through(self):
                    # bypasses the handle to the raw connection, no lock held
                    self._db._conn.execute("DELETE FROM t")

                def read_lock(self):
                    with self._lock:
                        return self._conn.execute("SELECT 1").fetchone()

                def emit_try(self):
                    with self._db.try_acquire() as acquired:
                        if acquired:
                            self._conn.execute("INSERT INTO t VALUES (1)")
                            self._conn.commit()

                @_synchronized
                def read_sync(self):
                    return self._conn.execute("SELECT 1").fetchone()

                def vacuum(self):
                    conn = connect()  # dedicated local connection, not self._conn
                    conn.execute("VACUUM")

                def write_after_lock(self):
                    self._conn.execute("PRAGMA x")  # bare, before the lock block
                    with self._lock:
                        self._conn.commit()

            class Other:
                def __init__(self):
                    self._conn.execute("DELETE FROM facts")  # bare in a 2nd class
            """
        )
        offenders = _bare_conn_sql_offenders(source, exempt_methods=set())
        named = {o.split(":")[0] for o in offenders}
        # Bare calls are caught (incl. self._db._conn reach-through and a bare
        # call in a SECOND class); guarded / synchronized / dedicated-conn are not.
        self.assertIn("S.read_bad", named)
        self.assertIn("S.reach_through", named)
        self.assertIn("S.write_after_lock", named)
        self.assertIn("Other.__init__", named)
        self.assertNotIn("S.read_lock", named)
        self.assertNotIn("S.emit_try", named)
        self.assertNotIn("S.read_sync", named)
        self.assertNotIn("S.vacuum", named)
        # Allowlist is keyed by Class.method: exempting one class's method does
        # NOT exempt a same-named method on another class.
        partial = _bare_conn_sql_offenders(
            source, exempt_methods={"S.read_bad", "S.reach_through", "S.write_after_lock"}
        )
        self.assertEqual({o.split(":")[0] for o in partial}, {"Other.__init__"})
        # Fully exempting every flagged Class.method clears the detector.
        cleared = _bare_conn_sql_offenders(
            source,
            exempt_methods={
                "S.read_bad",
                "S.reach_through",
                "S.write_after_lock",
                "Other.__init__",
            },
        )
        self.assertEqual(cleared, [])

    def test_no_bare_conn_execute_outside_runtimedb_cursor(self) -> None:
        """Every ``self._conn`` / ``self._db._conn`` SQL call in a RuntimeDb-backed
        store runs under the shared lock. The allowlist (keyed by ``Class.method``)
        names methods that are safe for one of two audited reasons (F1.1b read-lock
        audit): they run single-threaded at construction (schema/migration), or
        they are private helpers invoked only from a caller that already holds the
        lock (the ``_locked``/``_unlocked`` and ``_materialize_*`` helpers). A NEW
        store method doing bare ``self._conn`` SQL belongs UNDER the lock — not in
        this allowlist."""
        allowlist: dict[str, set[str]] = {
            "claw_v2/memory.py": {
                "MemoryStore.__init__",  # schema executescript, single-threaded at build
                "MemoryStore._migrate",  # one-time migrations, single-threaded at build
                # one-time outcomes-table migration; called only from _migrate
                "MemoryStore._ensure_task_outcome_usable_reply_unverified_locked",
                "MemoryStore._outcome_graph_neighbors",  # read helper; caller holds lock
                "MemoryStore._index_outcome_tags",  # write helper; caller holds lock
                "MemoryStore._update_session_state_locked",  # caller holds lock
                "MemoryStore._mark_provider_session_reset_locked",  # caller holds lock
                "MemoryStore._clear_provider_sessions_for_app_locked",  # caller holds lock
            },
            "claw_v2/observe.py": {
                "ObserveStream.__init__",  # schema executescript, single-threaded at build
                "ObserveStream._ensure_schema",  # one-time migration, single-threaded at build
            },
            "claw_v2/jobs.py": {
                "JobService._get_active_by_resume_key_unlocked",  # caller holds lock
                "JobService._migrate_resume_key_uniqueness",  # one-time migration under __init__ lock
            },
            "claw_v2/orchestration.py": {
                "OrchestrationStore._next_version_unlocked",  # caller holds lock
                "OrchestrationStore._insert_event_unlocked",  # caller holds lock
            },
            "claw_v2/task_ledger.py": {
                # one-time migration under __init__ lock
                "TaskLedger._ensure_completed_unverified_status_locked",
            },
            "claw_v2/capability_grants.py": set(),
            "claw_v2/property_graph.py": {
                "PropertyGraphProjection.ensure_schema",  # schema executescript, single-threaded
                "PropertyGraphProjection._materialize_tasks",  # read helper; materialize() holds lock
                "PropertyGraphProjection._materialize_observe_events",  # read helper; materialize() holds lock
                "PropertyGraphProjection._materialize_task_outcomes",  # read helper; materialize() holds lock
                "PropertyGraphProjection._materialize_facts",  # read helper; materialize() holds lock
                "PropertyGraphProjection._table_exists",  # read helper; materialize()/ensure_schema holds lock
            },
        }
        offenders: dict[str, list[str]] = {}
        for rel_path, exempt in allowlist.items():
            source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
            bad = _bare_conn_sql_offenders(source, exempt_methods=exempt)
            if bad:
                offenders[rel_path] = bad
        self.assertEqual(
            offenders,
            {},
            "bare self._conn SQL outside the shared lock (RAÍZ #1 read-lock "
            f"discipline regressed): {offenders}",
        )


class F2DurabilityArchitectureInvariantTests(unittest.TestCase):
    @staticmethod
    def _name_from_annotation(node: ast.AST | None) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""

    def test_f2_schema_uses_runtimedb_not_raw_sqlite_connections(self) -> None:
        path = REPO_ROOT / "claw_v2" / "f2_durability_schema.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        funcs = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "ensure_f2_durability_schema"
        ]

        self.assertEqual(len(funcs), 1)
        func = funcs[0]
        self.assertEqual([arg.arg for arg in func.args.args], ["runtime_db"])
        self.assertEqual(self._name_from_annotation(func.args.args[0].annotation), "RuntimeDb")
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "transaction"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "runtime_db"
                for node in ast.walk(func)
            )
        )

        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders.extend(alias.name for alias in node.names if alias.name == "sqlite3")
            elif isinstance(node, ast.ImportFrom):
                if node.module == "sqlite3":
                    offenders.append("from sqlite3 import ...")
                if node.module == "claw_v2.sqlite_runtime":
                    offenders.extend(
                        alias.name for alias in node.names if alias.name == "connect_runtime_sqlite"
                    )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "sqlite3"
            ):
                offenders.append("sqlite3.connect")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "connect_runtime_sqlite"
            ):
                offenders.append("connect_runtime_sqlite")
        self.assertEqual(offenders, [])

    def test_f2_store_is_runtimedb_backed_and_not_runtime_wired(self) -> None:
        path = REPO_ROOT / "claw_v2" / "f2_durability_store.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        classes = [
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "F2DurabilityStore"
        ]
        self.assertEqual(len(classes), 1)
        init = next(
            member
            for member in classes[0].body
            if isinstance(member, ast.FunctionDef) and member.name == "__init__"
        )
        self.assertEqual([arg.arg for arg in init.args.args], ["self", "runtime_db"])
        self.assertEqual(self._name_from_annotation(init.args.args[1].annotation), "RuntimeDb")
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ensure_f2_durability_schema"
                for node in ast.walk(init)
            )
        )

        lock_helper_calls: set[str] = set()
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in {"sqlite3", "claw_v2.task_handler", "claw_v2.coordinator"}:
                        offenders.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module in {"sqlite3", "claw_v2.task_handler", "claw_v2.coordinator"}:
                    offenders.append(f"from {node.module} import ...")
                if node.module == "claw_v2.sqlite_runtime":
                    offenders.extend(
                        alias.name for alias in node.names if alias.name == "connect_runtime_sqlite"
                    )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "sqlite3"
            ):
                offenders.append("sqlite3.connect")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "connect_runtime_sqlite"
            ):
                offenders.append("connect_runtime_sqlite")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"cursor", "transaction"}
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "_db"
                and isinstance(node.func.value.value, ast.Name)
                and node.func.value.value.id == "self"
            ):
                lock_helper_calls.add(node.func.attr)
        self.assertEqual(offenders, [])
        self.assertTrue({"cursor", "transaction"}.issubset(lock_helper_calls))

    def test_f2_0_does_not_wire_taskhandler_or_coordinator_checkpoint_writes(self) -> None:
        f2_symbols = {
            "ensure_f2_durability_schema",
            "phase_checkpoints",
            "phase_checkpoint_writes",
            "external_effect_records",
            "phase_recovery_cursors",
        }
        runtime_paths = ("claw_v2/task_handler.py", "claw_v2/coordinator.py")

        offenders: dict[str, list[str]] = {}
        for rel_path in runtime_paths:
            path = REPO_ROOT / rel_path
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            bad: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    bad.extend(
                        alias.name
                        for alias in node.names
                        if alias.name == "claw_v2.f2_durability_schema"
                    )
                elif isinstance(node, ast.ImportFrom):
                    if node.module == "claw_v2.f2_durability_schema":
                        bad.append("from claw_v2.f2_durability_schema import ...")
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "ensure_f2_durability_schema"
                ):
                    bad.append("ensure_f2_durability_schema()")
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"execute", "executemany", "executescript"}
                ):
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            bad.extend(
                                symbol for symbol in f2_symbols if symbol in arg.value
                            )
            if bad:
                offenders[rel_path] = bad
        self.assertEqual(
            offenders,
            {},
            "F2.0 must add schema/tests only; TaskHandler/Coordinator checkpoint "
            f"write wiring belongs to a later PR: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
