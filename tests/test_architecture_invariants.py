from __future__ import annotations

import ast
import inspect
import os
import tempfile
import textwrap
import unittest
from collections.abc import Mapping
from dataclasses import dataclass
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

# R2.0: these are the only scheduler jobs that are still known to perform
# blocking/FS/network/CDP/LLM work inline. This is not a migration list; it is a
# narrow, named residual so the tripwire can fail closed for any new inline
# blocking cron work while R2.x migrates the residual off-tick.
_ALLOWED_INLINE_BLOCKING_CRON_JOBS: dict[str, tuple[str, ...]] = {
    "heartbeat@claw_v2/main.py": ("handler attribute emit",),
    "daemon_heartbeat@claw_v2/lifecycle.py": (
        "claw_v2/lifecycle.py:run._emit_daemon_heartbeat:runtime_db_write_probe",
        "claw_v2/lifecycle.py:run._emit_daemon_heartbeat:write_liveness_heartbeat_record",
    ),
    "fitness_reminder@claw_v2/lifecycle.py": (
        "claw_v2/lifecycle.py:run._fitness_reminder:Path.mkdir",
        "claw_v2/lifecycle.py:run._fitness_reminder:Path.write_text",
        "claw_v2/lifecycle.py:run._fitness_reminder:claw_v2/lifecycle.py:should_send_fitness_reminder:Path.read_text",
    ),
    "morning_brief@claw_v2/lifecycle.py": (
        "claw_v2/morning_brief.py:MorningBriefService.run_if_due:claw_v2/morning_brief.py:MorningBriefService._mark_sent:Path.mkdir",
        "claw_v2/morning_brief.py:MorningBriefService.run_if_due:claw_v2/morning_brief.py:MorningBriefService._mark_sent:Path.write_text",
        "claw_v2/morning_brief.py:MorningBriefService.run_if_due:claw_v2/morning_brief.py:MorningBriefService.build_message:claw_v2/morning_brief.py:MorningBriefService._render_via_llm:router.ask",
        "claw_v2/morning_brief.py:MorningBriefService.run_if_due:claw_v2/morning_brief.py:should_send_morning_brief:Path.read_text",
    ),
    "evening_brief@claw_v2/lifecycle.py": (
        "claw_v2/morning_brief.py:MorningBriefService.run_if_due:claw_v2/morning_brief.py:MorningBriefService._mark_sent:Path.mkdir",
        "claw_v2/morning_brief.py:MorningBriefService.run_if_due:claw_v2/morning_brief.py:MorningBriefService._mark_sent:Path.write_text",
        "claw_v2/morning_brief.py:MorningBriefService.run_if_due:claw_v2/morning_brief.py:MorningBriefService.build_message:claw_v2/morning_brief.py:MorningBriefService._render_via_llm:router.ask",
        "claw_v2/morning_brief.py:MorningBriefService.run_if_due:claw_v2/morning_brief.py:should_send_morning_brief:Path.read_text",
    ),
    "notebooklm_orchestration_poll@claw_v2/lifecycle.py": ("poll_orchestrations",),
    "nlm_wiki_sync@claw_v2/lifecycle.py": ("ingest_from_notebooklm",),
    "wiki_lint@claw_v2/main.py": (
        "claw_v2/wiki.py:WikiService.lint:Path.read_text",
        "claw_v2/wiki.py:WikiService.lint:claw_v2/wiki.py:WikiService._append_log:Path.open",
        "claw_v2/wiki.py:WikiService.lint:claw_v2/wiki.py:WikiService._list_wiki_pages:claw_v2/wiki.py:WikiService._is_deprecated:Path.read_text",
    ),
    "wiki_confidence@claw_v2/main.py": (
        "claw_v2/wiki.py:WikiService.recompute_confidence:claw_v2/wiki.py:WikiService._append_log:Path.open",
        "claw_v2/wiki.py:WikiService.recompute_confidence:claw_v2/wiki.py:WikiService._compute_confidence:claw_v2/wiki.py:WikiService._extract_updated_date:Path.read_text",
        "claw_v2/wiki.py:WikiService.recompute_confidence:claw_v2/wiki.py:WikiService._compute_confidence:claw_v2/wiki.py:WikiService._list_wiki_pages:claw_v2/wiki.py:WikiService._is_deprecated:Path.read_text",
        "claw_v2/wiki.py:WikiService.recompute_confidence:claw_v2/wiki.py:WikiService._list_wiki_pages:claw_v2/wiki.py:WikiService._is_deprecated:Path.read_text",
        "claw_v2/wiki.py:WikiService.recompute_confidence:claw_v2/wiki.py:WikiService._set_frontmatter_field:Path.read_text",
        "claw_v2/wiki.py:WikiService.recompute_confidence:claw_v2/wiki.py:WikiService._set_frontmatter_field:Path.write_text",
    ),
}
_BLOCKING_HANDLER_ATTRIBUTES = frozenset({"emit", "run_if_due"})
_BLOCKING_CRON_CALL_NAMES = frozenset(
    {
        "_run_osascript",
        "ingest_from_notebooklm",
        "open",
        "poll_orchestrations",
        "run_external_summary_command",
        "run_subprocess_bounded",
        "runtime_db_write_probe",
        "write_liveness_heartbeat_record",
    }
)
_BLOCKING_FILESYSTEM_METHODS = frozenset(
    {
        "mkdir",
        "open",
        "read_bytes",
        "read_text",
        "replace",
        "rename",
        "rmdir",
        "touch",
        "unlink",
        "write_bytes",
        "write_text",
    }
)
_BLOCKING_SUBPROCESS_METHODS = frozenset({"call", "check_call", "check_output", "Popen", "run"})
_BLOCKING_HTTP_METHODS = frozenset(
    {"delete", "get", "head", "options", "patch", "post", "put", "request", "stream"}
)
_BLOCKING_WAIT_METHODS = frozenset({"join", "sleep", "wait"})
_BLOCKING_WAIT_TARGET_NAMES = frozenset(
    {"executor", "future", "fut", "process", "proc", "t", "thread", "worker"}
)


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
    def test_ui_open_app_and_inspect_app_patterns_are_whole_message_only(self) -> None:
        from claw_v2.bot_helpers import _TELEGRAM_IMPERATIVE_RULES

        offenders: list[str] = []
        for rule in _TELEGRAM_IMPERATIVE_RULES:
            if rule.get("intent") not in {"ui.open_app", "ui.inspect_app"}:
                continue
            for pattern in rule.get("patterns", ()):
                if not pattern.startswith(r"^\s*") or not pattern.endswith("$"):
                    offenders.append(f"{rule['intent']}:{pattern}")

        self.assertEqual(offenders, [])

    def test_runtime_db_self_heal_reconnect_is_lock_only(self) -> None:
        from claw_v2.sqlite_runtime import RuntimeDb

        source = inspect.getsource(RuntimeDb)
        handle_source = inspect.getsource(RuntimeDb._handle_sqlite_exception)
        self.assertIn("_is_sqlite_locked_error(exc)", handle_source)
        self.assertIn("_reconnect_after_persistent_lock(operation, exc)", handle_source)
        self.assertEqual(source.count("_reconnect_after_persistent_lock("), 2)

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

    def test_minimal_runtime_health_surface_is_shared_by_liveness_and_diagnostics(self) -> None:
        """O1.6 tripwire: the compact runtime health surface must stay on the
        existing liveness/diagnostics path, not drift into a parallel metrics
        stack or lose spill/degraded/db-probe fields."""
        from claw_v2 import liveness

        writer = (REPO_ROOT / "claw_v2" / "lifecycle.py").read_text(encoding="utf-8")
        reader = (REPO_ROOT / "claw_v2" / "diagnostics.py").read_text(encoding="utf-8")
        health_source = inspect.getsource(liveness.runtime_health_snapshot)
        spill_source = inspect.getsource(liveness.spill_pending_summary)
        self.assertEqual(liveness.RUNTIME_HEALTH_FIELD, "runtime_health")
        self.assertIn("liveness.runtime_health_snapshot", writer)
        self.assertIn("liveness.RUNTIME_HEALTH_FIELD", writer)
        self.assertIn("liveness.runtime_health_snapshot", reader)
        self.assertIn("liveness.RUNTIME_HEALTH_FIELD", reader)
        self.assertGreater(liveness.SPILL_PENDING_COUNT_MAX_LINES, 0)
        self.assertIn("max_lines", spill_source)
        self.assertIn("spill_pending_limited", spill_source)
        for field in (
            "spill_pending_count",
            "db_write_probe_status",
            "runtime_db_degraded_state",
        ):
            self.assertIn(field, health_source)
            self.assertIn(field, reader)

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

    def test_observe_spill_drain_only_runs_off_tick(self) -> None:
        """O1.4: spill replay can perform bounded SQLite writes and JSONL
        compaction, so it belongs in an off-tick background runner, not
        daemon.tick or a CronScheduler handler."""
        for rel in ("daemon.py", "cron.py"):
            src = (REPO_ROOT / "claw_v2" / rel).read_text(encoding="utf-8")
            self.assertNotIn("drain_spill", src, f"drain_spill must not appear in {rel}")

        tree = ast.parse((REPO_ROOT / "claw_v2" / "main.py").read_text(encoding="utf-8"))

        def _directly_calls_drain_spill(func: ast.AST) -> bool:
            stack = list(getattr(func, "body", []))
            while stack:
                node = stack.pop()
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "drain_spill"
                ):
                    return True
                stack.extend(ast.iter_child_nodes(node))
            return False

        drain_funcs = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and _directly_calls_drain_spill(node)
        }
        self.assertTrue(drain_funcs, "expected a main.py function calling drain_spill")

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

        unwired = drain_funcs - registered_handlers
        self.assertEqual(unwired, set(), f"drain_spill call-sites not off-tick: {unwired}")

    def test_audit_critical_observe_events_are_centrally_classified(self) -> None:
        """O1.5: audit-critical observe events must be marked before any
        RuntimeDb contention spill path, so policy/auth/tool/approval/critical
        failures cannot be silently fast-dropped as ordinary diagnostics."""
        from claw_v2.observe import (
            AUDIT_CRITICAL_OBSERVE_EVENTS,
            ObserveStream,
            is_audit_critical_event,
        )

        required = {
            "approval": {"approval_created", "approval_approved", "approval_rejected"},
            "human_authorization": {
                "owner_delegation_approval_required",
                "telegram_imperative_pending_approval",
                "implicit_approval_requires_explicit_approval",
                "approval_detected",
                "computer_approval_pending",
                "computer_browser_use_approval_required",
                "computer_approval_resume_blocked",
            },
            "tool_use": {
                "sdk_post_tool_use",
                "sdk_post_tool_use_failure",
                "runtime_policy_tool_not_declared",
            },
            "auth_policy": {"web_chat_auth_rejected", "tier3_approval_required"},
            "critical_errors": {
                "runtime_db_degraded",
                "daemon_branch_integrity_violation",
                "scheduled_job_error",
            },
        }
        missing = {
            category: sorted(events - AUDIT_CRITICAL_OBSERVE_EVENTS)
            for category, events in required.items()
            if events - AUDIT_CRITICAL_OBSERVE_EVENTS
        }
        self.assertEqual(missing, {})
        for event_type in set().union(*required.values()):
            self.assertTrue(is_audit_critical_event(event_type), event_type)
        self.assertFalse(is_audit_critical_event("daemon_background_runner_cycle"))

        emit_source = inspect.getsource(ObserveStream.emit)
        spill_source = inspect.getsource(ObserveStream._spill_dropped_event)
        self.assertIn("is_audit_critical_event", emit_source)
        self.assertIn("audit_critical", spill_source)

    def test_cron_inline_blocking_tripwire_has_teeth(self) -> None:
        sources = {
            "synthetic_helpers.py": textwrap.dedent(
                """
                import time
                from pathlib import Path

                class ExternalService:
                    def service_method(self):
                        self.stamp_path.write_text("x")

                    def join_method(self):
                        worker.join()

                """
            ),
            "synthetic.py": textwrap.dedent(
                """
            import httpx
            import subprocess
            import time
            from pathlib import Path

            from claw_v2.cron import ScheduledJob

            from synthetic_helpers import ExternalService

            def _wrap_job_handler(*, name, observe, handler, skip_if=None):
                return handler

            def _bad_http():
                httpx.get("https://example.com", timeout=5)

            def helper_sleep():
                time.sleep(1)

            def _bad_helper_delegation():
                helper_sleep()

            def _bad_filesystem():
                marker_path = Path("marker")
                marker_path.write_text("x")

            def _bad_subprocess():
                subprocess.run(["true"], timeout=1)

            def _bad_blocking():
                time.sleep(1)

            service = ExternalService()
            dynamic_name = "bad_dynamic"
            scheduler.register(ScheduledJob(name="bad_http", interval_seconds=60, handler=_bad_http))
            scheduler.register(
                ScheduledJob(
                    name="bad_helper_delegation",
                    interval_seconds=60,
                    handler=_bad_helper_delegation,
                )
            )
            scheduler.register(
                ScheduledJob(name="bad_filesystem", interval_seconds=60, handler=_bad_filesystem)
            )
            scheduler.register(
                ScheduledJob(name="bad_subprocess", interval_seconds=60, handler=_bad_subprocess)
            )
            scheduler.register(
                ScheduledJob(name="bad_blocking", interval_seconds=60, handler=_bad_blocking)
            )
            scheduler.register(
                ScheduledJob(
                    name="bad_wrapped_http",
                    interval_seconds=60,
                    handler=_wrap_job_handler(
                        name="bad_wrapped_http",
                        observe=None,
                        handler=lambda: httpx.post("https://example.com", timeout=5),
                    ),
                )
            )
            scheduler.register(ScheduledJob(name="bad_service_method", interval_seconds=60, handler=service.service_method))
            scheduler.register(ScheduledJob(name="bad_join_method", interval_seconds=60, handler=service.join_method))
            scheduler.register(ScheduledJob(name=dynamic_name, interval_seconds=60, handler=lambda: httpx.get("https://example.com")))
            """
            ),
        }

        offenders = _cron_inline_blocking_offenders_from_sources(sources)

        self.assertTrue(
            any(key.startswith("<dynamic>@synthetic.py:") for key in offenders),
            "dynamic ScheduledJob names must not skip handler analysis",
        )
        expected_literal_jobs = {
            "bad_blocking@synthetic.py",
            "bad_filesystem@synthetic.py",
            "bad_helper_delegation@synthetic.py",
            "bad_http@synthetic.py",
            "bad_join_method@synthetic.py",
            "bad_service_method@synthetic.py",
            "bad_subprocess@synthetic.py",
            "bad_wrapped_http@synthetic.py",
        }
        self.assertLessEqual(expected_literal_jobs, set(offenders))
        flattened = "\n".join("\n".join(reasons) for reasons in offenders.values())
        for expected in (
            "httpx.get",
            "Path.write_text",
            "subprocess.run",
            "time.sleep",
            "synthetic_helpers.py:ExternalService.service_method:Path.write_text",
            "synthetic.py:helper_sleep:time.sleep",
            "blocking.join",
        ):
            self.assertIn(expected, flattened)

    def test_cron_inline_blocking_residual_is_explicit_and_minimal(self) -> None:
        sources = {
            str(path.relative_to(REPO_ROOT)): path.read_text(encoding="utf-8")
            for path in _package_python_files()
        }

        detected = _cron_inline_blocking_offenders_from_sources(sources)
        self.assertEqual(
            detected,
            {key: list(reasons) for key, reasons in _ALLOWED_INLINE_BLOCKING_CRON_JOBS.items()},
        )

    def test_no_default_on_scheduler_job_runs_heavy_work_inline_in_daemon_tick(self) -> None:
        """Deny-by-default backstop for Core Invariant 1.

        Builds the runtime at PRODUCTION DEFAULT (no EVAL_ON_SELF_IMPROVE
        override) and sweeps EVERY registered scheduler job, invoking each
        handler under sentinels on the heavy chokepoints (provider LLM via
        ``router.ask``, the self-improve experiment loop, sub-agent dispatch,
        and any subprocess). The static tripwire above covers direct HTTP,
        filesystem, and blocking/sleep calls that this runtime sweep cannot
        safely patch globally. A job that trips a sentinel ran heavy work inline
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


def _cron_inline_blocking_offenders_from_sources(
    sources: Mapping[str, str],
) -> dict[str, list[str]]:
    index = _CronSourceIndex.from_sources(sources)
    offenders: dict[str, list[str]] = {}
    for rel_path, tree in index.trees.items():
        for call in (node for node in ast.walk(tree) if _is_scheduled_job_call(node)):
            assert isinstance(call, ast.Call)
            handler = _keyword_value(call, "handler")
            if handler is None:
                continue
            reasons = _blocking_cron_handler_reasons(handler, index=index, rel_path=rel_path)
            if reasons:
                offenders[_scheduled_job_key(call, rel_path)] = sorted(reasons)
    return offenders


def _blocking_cron_handler_reasons(
    handler: ast.AST,
    *,
    index: "_CronSourceIndex",
    rel_path: str,
) -> list[str]:
    if isinstance(handler, ast.Call) and _call_name(handler) == "_wrap_job_handler":
        wrapped = _keyword_value(handler, "handler")
        if wrapped is None:
            return ["_wrap_job_handler missing handler keyword"]
        return _blocking_cron_handler_reasons(wrapped, index=index, rel_path=rel_path)
    if isinstance(handler, ast.Name):
        return sorted(
            {
                reason
                for entry in index.functions_named(handler.id, rel_path=rel_path)
                for reason in index.blocking_reasons_for_entry(entry)
            }
        )
    if isinstance(handler, ast.Lambda):
        return _blocking_reasons_in_nodes([handler.body], index=index, rel_path=rel_path)
    if isinstance(handler, ast.Attribute):
        entries = index.methods_named(handler.attr)
        if len(entries) == 1:
            return sorted(index.blocking_reasons_for_entry(entries[0]))
        if handler.attr in _BLOCKING_HANDLER_ATTRIBUTES:
            return [f"handler attribute {handler.attr}"]
        return sorted(
            {reason for entry in entries for reason in index.blocking_reasons_for_entry(entry)}
        )
    return _blocking_reasons_in_nodes([handler], index=index, rel_path=rel_path)


def _is_scheduled_job_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _call_name(node) in {"ScheduledJob", "_SJ"}


def _scheduled_job_key(node: ast.Call, rel_path: str) -> str:
    job_name = _literal_string(_keyword_value(node, "name"))
    if job_name:
        return f"{job_name}@{rel_path}"
    return f"<dynamic>@{rel_path}:{node.lineno}"


@dataclass(frozen=True)
class _CronFunctionEntry:
    rel_path: str
    name: str
    qualname: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    class_name: str | None = None


class _CronSourceIndex:
    def __init__(
        self,
        *,
        trees: dict[str, ast.Module],
        entries: list[_CronFunctionEntry],
    ) -> None:
        self.trees = trees
        self.entries = entries
        self._entries_by_file_name: dict[tuple[str, str], list[_CronFunctionEntry]] = {}
        self._entries_by_name: dict[str, list[_CronFunctionEntry]] = {}
        self._methods_by_name: dict[str, list[_CronFunctionEntry]] = {}
        self._methods_by_class: dict[tuple[str, str, str], list[_CronFunctionEntry]] = {}
        for entry in entries:
            self._entries_by_file_name.setdefault((entry.rel_path, entry.name), []).append(entry)
            self._entries_by_name.setdefault(entry.name, []).append(entry)
            if entry.class_name is not None:
                self._methods_by_name.setdefault(entry.name, []).append(entry)
                self._methods_by_class.setdefault(
                    (entry.rel_path, entry.class_name, entry.name), []
                ).append(entry)

    @classmethod
    def from_sources(cls, sources: Mapping[str, str]) -> "_CronSourceIndex":
        trees = {
            rel_path: ast.parse(source, filename=rel_path) for rel_path, source in sources.items()
        }
        entries: list[_CronFunctionEntry] = []
        for rel_path, tree in trees.items():
            entries.extend(_collect_cron_function_entries(tree, rel_path=rel_path))
        return cls(trees=trees, entries=entries)

    def functions_named(self, name: str, *, rel_path: str) -> list[_CronFunctionEntry]:
        return self._entries_by_file_name.get((rel_path, name), [])

    def methods_named(self, name: str) -> list[_CronFunctionEntry]:
        return self._methods_by_name.get(name, [])

    def methods_for_class(
        self, entry: _CronFunctionEntry, method_name: str
    ) -> list[_CronFunctionEntry]:
        if entry.class_name is None:
            return []
        return self._methods_by_class.get((entry.rel_path, entry.class_name, method_name), [])

    def blocking_reasons_for_entry(
        self,
        entry: _CronFunctionEntry,
        *,
        visited: frozenset[tuple[str, str]] = frozenset(),
    ) -> set[str]:
        key = (entry.rel_path, entry.qualname)
        if key in visited:
            return set()
        visited = visited | {key}
        reasons = _blocking_reasons_in_nodes(
            list(entry.node.body),
            index=self,
            rel_path=entry.rel_path,
            current_entry=entry,
            visited=visited,
        )
        return {f"{entry.rel_path}:{entry.qualname}:{reason}" for reason in reasons}


def _collect_cron_function_entries(
    tree: ast.Module,
    *,
    rel_path: str,
) -> list[_CronFunctionEntry]:
    entries: list[_CronFunctionEntry] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []
            self.class_stack: list[str] = []
            self.function_depth = 0

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.scope.append(node.name)
            self.class_stack.append(node.name)
            self.generic_visit(node)
            self.class_stack.pop()
            self.scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            class_name = (
                self.class_stack[-1] if self.class_stack and self.function_depth == 0 else None
            )
            qualname = ".".join((*self.scope, node.name))
            entries.append(
                _CronFunctionEntry(
                    rel_path=rel_path,
                    name=node.name,
                    qualname=qualname,
                    node=node,
                    class_name=class_name,
                )
            )
            self.scope.append(node.name)
            self.function_depth += 1
            self.generic_visit(node)
            self.function_depth -= 1
            self.scope.pop()

    _Visitor().visit(tree)
    return entries


def _blocking_reasons_in_nodes(
    nodes: list[ast.AST],
    *,
    index: _CronSourceIndex,
    rel_path: str,
    current_entry: _CronFunctionEntry | None = None,
    visited: frozenset[tuple[str, str]] = frozenset(),
) -> list[str]:
    reasons: list[str] = []
    path_names = _path_like_names(nodes)
    if current_entry is not None:
        path_names |= _function_path_like_arg_names(current_entry.node)
    stack = list(nodes)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            reason = _blocking_cron_call_reason(node, path_names=path_names)
            if reason is not None:
                reasons.append(reason)
            else:
                for target in _helper_call_targets(
                    node,
                    index=index,
                    rel_path=rel_path,
                    current_entry=current_entry,
                ):
                    reasons.extend(index.blocking_reasons_for_entry(target, visited=visited))
        stack.extend(ast.iter_child_nodes(node))
    return sorted(set(reasons))


def _helper_call_targets(
    node: ast.Call,
    *,
    index: _CronSourceIndex,
    rel_path: str,
    current_entry: _CronFunctionEntry | None,
) -> list[_CronFunctionEntry]:
    func = node.func
    if isinstance(func, ast.Name):
        return index.functions_named(func.id, rel_path=rel_path)
    if not isinstance(func, ast.Attribute):
        return []
    if isinstance(func.value, ast.Name) and func.value.id == "self" and current_entry is not None:
        return index.methods_for_class(current_entry, func.attr)
    return []


def _path_like_names(nodes: list[ast.AST]) -> set[str]:
    names: set[str] = set()
    for node in nodes:
        for child in ast.walk(node):
            if isinstance(child, ast.For) and _expr_yields_path_like(child.iter, names):
                for target in _name_targets(child.target):
                    names.add(target)
            value: ast.AST | None = None
            targets: list[ast.AST] = []
            if isinstance(child, ast.Assign):
                value = child.value
                targets = list(child.targets)
            elif isinstance(child, ast.AnnAssign):
                value = child.value
                targets = [child.target]
            if value is None or not _expr_is_path_like(value, names):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _name_targets(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Tuple | ast.List):
        return {name for item in node.elts for name in _name_targets(item)}
    return set()


def _expr_yields_path_like(node: ast.AST, path_names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in path_names or node.id.lower().endswith(("paths", "files", "pages"))
    if not isinstance(node, ast.Call):
        return False
    call_name = _call_name(node)
    return call_name.endswith(("_paths", "_files", "_pages")) or call_name in {
        "_list_wiki_pages",
        "glob",
        "rglob",
    }


def _function_path_like_arg_names(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> set[str]:
    args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    return {
        arg.arg
        for arg in args
        if arg.annotation is not None and _annotation_is_path_like(arg.annotation)
    }


def _annotation_is_path_like(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "Path"
    if isinstance(node, ast.Attribute):
        return node.attr == "Path"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value == "Path" or node.value.endswith(".Path")
    return False


def _expr_is_path_like(node: ast.AST, path_names: set[str]) -> bool:
    if isinstance(node, ast.Call) and _call_name(node) == "Path":
        return True
    if isinstance(node, ast.Name):
        return node.id in path_names or _name_looks_path_like(node.id)
    if isinstance(node, ast.Attribute):
        if _name_looks_path_like(node.attr):
            return True
        return node.attr == "parent" and _expr_is_path_like(node.value, path_names)
    return False


def _blocking_cron_call_reason(node: ast.Call, *, path_names: set[str]) -> str | None:
    dotted = _dotted_call_name(node)
    call_name = _call_name(node)
    attr_name = node.func.attr if isinstance(node.func, ast.Attribute) else ""
    if dotted.startswith("httpx.") and attr_name in _BLOCKING_HTTP_METHODS:
        return dotted
    if dotted.startswith("requests.") and attr_name in _BLOCKING_HTTP_METHODS:
        return dotted
    if dotted in {"urllib.request.urlopen", "urlopen"} or call_name == "urlopen":
        return "urllib.request.urlopen"
    if dotted.startswith("subprocess.") and attr_name in _BLOCKING_SUBPROCESS_METHODS:
        return dotted
    if attr_name in _BLOCKING_FILESYSTEM_METHODS and _looks_like_path_filesystem_call(
        node, path_names
    ):
        return f"Path.{attr_name}"
    if attr_name == "ask" and any(part.endswith("router") for part in dotted.split(".")):
        return "router.ask"
    if call_name in _BLOCKING_CRON_CALL_NAMES:
        return call_name
    if dotted in {"time.sleep", "asyncio.run"}:
        return dotted
    if attr_name in _BLOCKING_WAIT_METHODS and _looks_like_blocking_wait_call(node):
        return f"blocking.{attr_name}"
    return None


def _dotted_call_name(node: ast.Call) -> str:
    parts: list[str] = []
    value: ast.AST = node.func
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if isinstance(value, ast.Name):
        parts.append(value.id)
    return ".".join(reversed(parts))


def _looks_like_path_filesystem_call(node: ast.Call, path_names: set[str]) -> bool:
    if not isinstance(node.func, ast.Attribute):
        return False
    return _expr_is_path_like(node.func.value, path_names)


def _name_looks_path_like(name: str) -> bool:
    return name.lower().endswith(("_path", "_stamp", "stamp", "path"))


def _looks_like_blocking_wait_call(node: ast.Call) -> bool:
    if not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr == "sleep":
        return True
    target = node.func.value
    return isinstance(target, ast.Name) and target.id in _BLOCKING_WAIT_TARGET_NAMES


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
                # spill drain insert+marker helper; callers acquire self._lock
                # or RuntimeDb.try_acquire before invoking it.
                "ObserveStream._insert_spill_record_locked",
            },
            "claw_v2/jobs.py": {
                "JobService._get_active_by_resume_key_unlocked",  # caller holds lock
                "JobService._migrate_resume_key_uniqueness",  # one-time migration under __init__ lock
                "JobService._ensure_lease_columns",  # one-time migration under __init__ lock
                "JobService._update_row_to_running_with_lease",  # caller holds transaction lock
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
            if isinstance(node, ast.FunctionDef) and node.name == "ensure_f2_durability_schema"
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
        schema_ready = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_ensure_schema_ready"
        )
        self.assertEqual([arg.arg for arg in init.args.args], ["self", "runtime_db"])
        self.assertEqual(self._name_from_annotation(init.args.args[1].annotation), "RuntimeDb")
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_ensure_schema_ready"
                for node in ast.walk(init)
            )
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ensure_f2_durability_schema"
                for node in ast.walk(schema_ready)
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
                            bad.extend(symbol for symbol in f2_symbols if symbol in arg.value)
            if bad:
                offenders[rel_path] = bad
        self.assertEqual(
            offenders,
            {},
            "F2.0 must add schema/tests only; TaskHandler/Coordinator checkpoint "
            f"write wiring belongs to a later PR: {offenders}",
        )

    def test_f2_2_checkpoint_writes_are_flag_gated_without_recovery_or_effects(self) -> None:
        config_source = (REPO_ROOT / "claw_v2" / "config.py").read_text(encoding="utf-8")
        main_source = (REPO_ROOT / "claw_v2" / "main.py").read_text(encoding="utf-8")
        coordinator_source = (REPO_ROOT / "claw_v2" / "coordinator.py").read_text(encoding="utf-8")
        task_handler_source = (REPO_ROOT / "claw_v2" / "task_handler.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("f2_durability_enabled: bool = False", config_source)
        self.assertIn("CLAW_F2_DURABILITY_ENABLED", config_source)
        self.assertIn(
            "F2DurabilityStore(runtime_db) if config.f2_durability_enabled else None", main_source
        )
        self.assertIn("f2_durability_store=f2_durability_store", main_source)
        self.assertIn("if self.f2_durability_store is None:", coordinator_source)
        self.assertIn("f2_durability_write_failed", coordinator_source)

        forbidden_symbols = (
            "record_external_effect",
            "get_external_effect_by_idempotency_key",
            "update_external_effect_status",
            "upsert_recovery_cursor",
            "get_recovery_cursor",
        )
        offenders = {
            "claw_v2/coordinator.py": [
                symbol for symbol in forbidden_symbols if symbol in coordinator_source
            ],
            "claw_v2/task_handler.py": [
                symbol
                for symbol in ("F2DurabilityStore", *forbidden_symbols)
                if symbol in task_handler_source
            ],
        }
        self.assertEqual(
            {path: symbols for path, symbols in offenders.items() if symbols},
            {},
            "F2.2 may write flag-gated phase checkpoints only; recovery and "
            f"external-effect execution wiring belongs to later PRs: {offenders}",
        )

    def test_f2_4a_recovery_planner_is_classification_only(self) -> None:
        path = REPO_ROOT / "claw_v2" / "f2_recovery.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))

        self.assertIn(
            'F2_COORDINATOR_PHASES = ("research", "synthesis", "implementation", "verification")',
            source,
        )
        self.assertIn("will_replay_external_effects=False", source)
        self.assertNotIn("CLAW_F2_DURABILITY_ENABLED", source)
        self.assertNotIn("F2_DURABILITY_ENABLED", source)

        forbidden_imports = {
            "sqlite3",
            "claw_v2.config",
            "claw_v2.main",
            "claw_v2.browser",
            "claw_v2.browser_tools",
            "claw_v2.chrome",
            "claw_v2.chrome_handler",
            "claw_v2.computer",
            "claw_v2.computer_handler",
            "claw_v2.tools",
            "subprocess",
        }
        forbidden_names = {
            "record_external_effect",
            "update_external_effect_status",
            "append_checkpoint_write",
            "create_phase_checkpoint",
            "connect_runtime_sqlite",
            "delegate_task",
            "ThreadPoolExecutor",
            "Popen",
            "run",
        }
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders.extend(
                    alias.name for alias in node.names if alias.name in forbidden_imports
                )
            elif isinstance(node, ast.ImportFrom):
                if node.module in forbidden_imports:
                    offenders.append(f"from {node.module} import ...")
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in forbidden_names:
                    offenders.append(node.func.id)
                elif isinstance(node.func, ast.Attribute) and node.func.attr in forbidden_names:
                    offenders.append(node.func.attr)
                elif (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "ask"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "router"
                ):
                    offenders.append("router.ask")
        self.assertEqual(offenders, [])

    def test_f2_4b_taskhandler_consumes_planner_without_effect_execution(self) -> None:
        from claw_v2.task_handler import TaskHandler

        task_handler_source = (REPO_ROOT / "claw_v2" / "task_handler.py").read_text(
            encoding="utf-8"
        )
        coordinator_source = (REPO_ROOT / "claw_v2" / "coordinator.py").read_text(encoding="utf-8")

        self.assertIn("plan_f2_recovery", task_handler_source)
        self.assertIn("persist_cursor=False", task_handler_source)
        self.assertIn(
            '("research", "synthesis", "implementation", "verification")', task_handler_source
        )
        self.assertIn(
            'PHASE_ORDER = ("research", "synthesis", "implementation", "verification")',
            coordinator_source,
        )
        self.assertNotIn("plan_f2_recovery", coordinator_source)

        f2_helper_source = "\n".join(
            inspect.getsource(getattr(TaskHandler, name))
            for name in (
                "_f2_recovery_result_or_start_phase",
                "_f2_retryable_block_reason",
                "_f2_recovery_no_run_result",
            )
        )
        forbidden_symbols = (
            "record_external_effect",
            "get_external_effect_by_idempotency_key",
            "update_external_effect_status",
            "create_phase_checkpoint",
            "append_checkpoint_write",
            "upsert_recovery_cursor",
            "BrowserUseService",
            "ComputerService",
            "delegate_task",
            "dynamic_fanout",
        )
        offenders = {
            "TaskHandler F2.4B helpers": [
                symbol for symbol in forbidden_symbols if symbol in f2_helper_source
            ],
            "claw_v2/coordinator.py": [
                symbol
                for symbol in (
                    "record_external_effect",
                    "get_external_effect_by_idempotency_key",
                    "update_external_effect_status",
                    "dynamic_fanout",
                )
                if symbol in coordinator_source
            ],
        }
        self.assertEqual(
            {path: symbols for path, symbols in offenders.items() if symbols},
            {},
            "F2.4B may consume the recovery classifier only; side-effect wiring, "
            f"cursor persistence, and dynamic fanout are out of scope: {offenders}",
        )

    def test_f2_5a_diagnostics_collector_is_read_only_and_local(self) -> None:
        import claw_v2.diagnostics as diagnostics_module

        path = REPO_ROOT / "claw_v2" / "diagnostics.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))

        self.assertIn("def collect_f2_recovery_report", source)
        self.assertIn("mode=ro", inspect.getsource(diagnostics_module._open_readonly_sqlite))
        self.assertNotIn("mode=rw", inspect.getsource(diagnostics_module._open_readonly_sqlite))
        self.assertNotIn("mode=rwc", inspect.getsource(diagnostics_module._open_readonly_sqlite))
        self.assertNotIn("from claw_v2.sqlite_runtime", source)
        self.assertNotIn("from claw_v2.f2_durability_store", source)
        self.assertNotIn("from claw_v2.task_handler", source)
        self.assertNotIn("from claw_v2.coordinator", source)

        f2_function_names = {
            "collect_f2_recovery_report",
            "_collect_f2_recovery_cli_report",
            "_open_readonly_sqlite",
            "_empty_f2_report",
            "_empty_f2_counts",
            "_empty_f2_recent_records",
            "_safe_f2_text_summary",
            "format_f2_recovery_text",
        }
        f2_function_nodes = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and (node.name in f2_function_names or node.name.startswith("_f2_"))
        ]
        self.assertTrue(f2_function_nodes)

        forbidden_call_names = {
            "RuntimeDb",
            "F2DurabilityStore",
            "ensure_f2_durability_schema",
            "plan_f2_recovery",
            "TaskHandler",
            "Coordinator",
            "CoordinatorService",
            "connect_runtime_sqlite",
            "record_external_effect",
            "update_external_effect_status",
            "append_checkpoint_write",
            "create_phase_checkpoint",
            "upsert_recovery_cursor",
            "get_recovery_cursor",
            "BrowserUseService",
            "ComputerService",
            "delegate_task",
            "dynamic_fanout",
            "Popen",
        }
        forbidden_write_methods = {"commit", "rollback", "executescript", "executemany"}
        offenders: list[str] = []
        for function_node in f2_function_nodes:
            for node in ast.walk(function_node):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name) and node.func.id in forbidden_call_names:
                    offenders.append(f"{function_node.name}:{node.func.id}")
                elif isinstance(node.func, ast.Attribute):
                    if node.func.attr in forbidden_call_names | forbidden_write_methods:
                        offenders.append(f"{function_node.name}:{node.func.attr}")
        self.assertEqual(offenders, [])

        payload_policy_source = inspect.getsource(diagnostics_module._empty_f2_report)
        self.assertIn("raw_payloads_included", payload_policy_source)
        self.assertIn("False", payload_policy_source)
        cli_source = inspect.getsource(diagnostics_module._collect_f2_recovery_cli_report)
        self.assertIn("collect_f2_recovery_report", cli_source)
        self.assertNotIn("DEFAULT_DB_PATH", cli_source)
        self.assertNotIn("os.getenv", cli_source)
        main_source = inspect.getsource(diagnostics_module.main)
        self.assertIn("--f2-recovery-report", main_source)
        self.assertIn("--f2-db", main_source)
        self.assertIn("parser.error", main_source)


if __name__ == "__main__":
    unittest.main()
