from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from claw_v2.adapters.base import AdapterError
from claw_v2.agents import (
    ExperimentEvaluation,
    GitBranchPromotionExecutor,
    GitCommitPromotionExecutor,
    GitWorktreeExperimentRunner,
    PromotionManifest,
    PromotionToolingError,
    PromotionToolingGate,
    WorkspacePromotionExecutor,
)
from claw_v2.observe import ObserveStream
from claw_v2.types import CriticalActionExecution, CriticalActionVerification, LLMResponse


class FakeRouter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def ask(self, prompt: str, **kwargs) -> LLMResponse:
        self.calls.append({"prompt": prompt, **kwargs})
        worktree = Path(kwargs["cwd"])
        (worktree / "experiment.txt").write_text("changed", encoding="utf-8")
        return LLMResponse(
            content="Applied one incremental change.",
            lane="worker",
            provider="anthropic",
            model="claude-sonnet-4-5",
            cost_estimate=0.05,
        )


class FakeBrain:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def execute_critical_action(self, **kwargs) -> CriticalActionExecution:
        self.calls.append(kwargs)
        result = kwargs["executor"]()
        return CriticalActionExecution(
            action=kwargs["action"],
            status="executed",
            executed=True,
            result=result,
            verification=CriticalActionVerification(
                recommendation="approve",
                risk_level="low",
                summary="safe",
                should_proceed=True,
            ),
        )


class FakeDockerSandbox:
    def __init__(self, available: bool = True) -> None:
        self.available = available

    def is_available(self) -> bool:
        return self.available


class FakePromotionCommandRunner:
    def __init__(self, *, fail: dict[str, int] | None = None) -> None:
        self.fail = fail or {}
        self.calls: list[dict] = []

    def __call__(self, args, **kwargs) -> subprocess.CompletedProcess[str]:
        command = [str(arg) for arg in args]
        self.calls.append({"args": command, **kwargs})
        key = " ".join(command[:3])
        full_key = " ".join(command)
        returncode = self.fail.get(full_key, self.fail.get(key, 0))
        return subprocess.CompletedProcess(command, returncode, "", "tool output" if returncode else "")


class WorktreeRunnerTests(unittest.TestCase):
    def test_workspace_promotion_executor_applies_add_modify_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            (repo / "modify.txt").write_text("before\n", encoding="utf-8")
            (repo / "delete.txt").write_text("gone\n", encoding="utf-8")
            worktree = root / "worktree"
            worktree.mkdir()
            (worktree / "modify.txt").write_text("after\n", encoding="utf-8")
            (worktree / "add.txt").write_text("new\n", encoding="utf-8")

            executor = WorkspacePromotionExecutor(repo)
            result = executor(worktree, {}, "diff")

            self.assertEqual(result.manifest.modified, ["modify.txt"])
            self.assertEqual(result.manifest.added, ["add.txt"])
            self.assertEqual(result.manifest.deleted, ["delete.txt"])
            self.assertEqual((repo / "modify.txt").read_text(encoding="utf-8"), "after\n")
            self.assertEqual((repo / "add.txt").read_text(encoding="utf-8"), "new\n")
            self.assertFalse((repo / "delete.txt").exists())

    def test_worktree_runner_uses_disposable_git_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)

            router = FakeRouter()
            runner = GitWorktreeExperimentRunner(
                repo_root=repo,
                worktree_root=root / "worktrees",
                router=router,
                evaluator=lambda path, state, diff: ExperimentEvaluation(0.75, "improved", "0.75"),
            )
            record = runner(
                "agent-a",
                1,
                {"instruction": "Change the workspace", "allowed_tools": ["Write"], "last_verified_state": {"metric": 0.5}},
            )

            self.assertEqual(record.metric_value, 0.75)
            self.assertEqual(record.status, "improved")
            self.assertEqual(router.calls[0]["cwd"], str(root / "worktrees" / "agent-a" / "exp-1"))
            self.assertFalse((repo / "experiment.txt").exists())
            self.assertFalse((root / "worktrees" / "agent-a" / "exp-1").exists())

    def test_worktree_runner_emits_replayable_observe_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)
            observe = ObserveStream(root / "observe.db")

            runner = GitWorktreeExperimentRunner(
                repo_root=repo,
                worktree_root=root / "worktrees",
                router=FakeRouter(),
                evaluator=lambda path, state, diff: ExperimentEvaluation(0.75, "improved", "0.75"),
                docker_sandbox=FakeDockerSandbox(),
                observe=observe,
            )
            record = runner(
                "agent-observed",
                1,
                {
                    "instruction": "Change the workspace",
                    "allowed_tools": ["Write"],
                    "last_verified_state": {"metric": 0.5},
                    "session_id": "observed-session",
                },
            )

            self.assertEqual(record.status, "improved")
            events = observe.recent_events(limit=5)
            self.assertEqual(events[0]["event_type"], "worktree_experiment_completed")
            self.assertEqual(events[1]["event_type"], "worktree_experiment_started")
            self.assertEqual(events[0]["trace_id"], events[1]["trace_id"])
            self.assertEqual(events[0]["payload"]["session_id"], "observed-session")
            self.assertEqual(events[0]["payload"]["workspace_mode"], "git_worktree")
            self.assertTrue(events[0]["payload"]["docker_available"])
            self.assertFalse(events[0]["payload"]["promotion_enabled"])
            self.assertEqual(events[0]["payload"]["metric_value"], 0.75)
            self.assertFalse(events[0]["payload"]["worktree_preserved"])
            trace_events = observe.trace_events(events[0]["trace_id"])
            self.assertEqual(
                [event["event_type"] for event in trace_events],
                ["worktree_experiment_started", "worktree_experiment_completed"],
            )

    def test_worktree_runner_preserves_workspace_on_adapter_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)

            class TimeoutRouter(FakeRouter):
                def ask(self, prompt: str, **kwargs) -> LLMResponse:
                    self.calls.append({"prompt": prompt, **kwargs})
                    worktree = Path(kwargs["cwd"])
                    (worktree / "partial.txt").write_text("partial\n", encoding="utf-8")
                    raise AdapterError("Codex CLI timed out after 300.0s")

            router = TimeoutRouter()
            runner = GitWorktreeExperimentRunner(
                repo_root=repo,
                worktree_root=root / "worktrees",
                router=router,
                evaluator=lambda path, state, diff: ExperimentEvaluation(0.75, "improved", "0.75"),
            )

            with self.assertRaisesRegex(AdapterError, "preserved experiment workspace"):
                runner(
                    "agent-timeout",
                    1,
                    {
                        "instruction": "Change the workspace",
                        "allowed_tools": ["Write"],
                        "last_verified_state": {"metric": 0.5},
                    },
                )

            preserved = root / "worktrees" / "agent-timeout" / "exp-1"
            self.assertTrue(preserved.exists())
            self.assertEqual((preserved / "partial.txt").read_text(encoding="utf-8"), "partial\n")
            self.assertFalse((repo / "partial.txt").exists())

    def test_worktree_runner_emits_failed_event_when_workspace_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)
            observe = ObserveStream(root / "observe.db")

            class TimeoutRouter(FakeRouter):
                def ask(self, prompt: str, **kwargs) -> LLMResponse:
                    worktree = Path(kwargs["cwd"])
                    (worktree / "partial.txt").write_text("partial\n", encoding="utf-8")
                    raise AdapterError("Codex CLI timed out after 300.0s")

            runner = GitWorktreeExperimentRunner(
                repo_root=repo,
                worktree_root=root / "worktrees",
                router=TimeoutRouter(),
                evaluator=lambda path, state, diff: ExperimentEvaluation(0.75, "improved", "0.75"),
                docker_sandbox=FakeDockerSandbox(),
                observe=observe,
            )

            with self.assertRaisesRegex(AdapterError, "preserved experiment workspace"):
                runner(
                    "agent-observed-failure",
                    1,
                    {
                        "instruction": "Change the workspace",
                        "allowed_tools": ["Write"],
                        "last_verified_state": {"metric": 0.5},
                    },
                )

            events = observe.recent_events(limit=5)
            self.assertEqual(events[0]["event_type"], "worktree_experiment_failed")
            self.assertEqual(events[1]["event_type"], "worktree_experiment_started")
            self.assertEqual(events[0]["trace_id"], events[1]["trace_id"])
            self.assertEqual(events[0]["payload"]["session_id"], "auto-research:agent-observed-failure")
            self.assertEqual(events[0]["payload"]["error_type"], "AdapterError")
            self.assertTrue(events[0]["payload"]["worktree_preserved"])
            self.assertTrue((root / "worktrees" / "agent-observed-failure" / "exp-1").exists())

    def test_worktree_runner_does_not_preserve_when_preparation_fails(self) -> None:
        # Review blocker 1: if _prepare_workspace fails (workspace_mode stays
        # "unprepared") there is nothing useful to preserve, and a leftover
        # partial/stale worktree auto-perpetuates the exit-128 collision. The
        # run must clean the partial workspace and re-raise the ORIGINAL error,
        # not claim it preserved a workspace.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)
            observe = ObserveStream(root / "observe.db")

            class FailingPrepRunner(GitWorktreeExperimentRunner):
                def _prepare_workspace(self, worktree_path: Path) -> str:
                    worktree_path.mkdir(parents=True, exist_ok=True)
                    (worktree_path / "stale.txt").write_text("stale\n", encoding="utf-8")
                    raise AdapterError("git worktree add failed (exit 128) for stale path")

            runner = FailingPrepRunner(
                repo_root=repo,
                worktree_root=root / "worktrees",
                router=FakeRouter(),
                evaluator=lambda path, state, diff: ExperimentEvaluation(0.75, "improved", "0.75"),
                docker_sandbox=FakeDockerSandbox(),
                observe=observe,
            )

            with self.assertRaises(AdapterError) as ctx:
                runner(
                    "agent-prep-fail",
                    1,
                    {"instruction": "x", "allowed_tools": ["Write"], "last_verified_state": {"metric": 0.5}},
                )

            self.assertNotIn("preserved experiment workspace", str(ctx.exception))
            self.assertFalse((root / "worktrees" / "agent-prep-fail" / "exp-1").exists())
            events = observe.recent_events(limit=5)
            self.assertEqual(events[0]["event_type"], "worktree_experiment_failed")
            self.assertEqual(events[0]["payload"]["workspace_mode"], "unprepared")
            self.assertFalse(events[0]["payload"]["worktree_preserved"])

    def test_worktree_runner_threads_trace_context_to_worker_ask(self) -> None:
        # Review blocker 2: the worker LLMRouter.ask must receive the experiment
        # trace in evidence_pack so the llm_response/cost correlates with the
        # worktree_experiment_* events in `claw think trace`.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)
            observe = ObserveStream(root / "observe.db")

            router = FakeRouter()
            runner = GitWorktreeExperimentRunner(
                repo_root=repo,
                worktree_root=root / "worktrees",
                router=router,
                evaluator=lambda path, state, diff: ExperimentEvaluation(0.75, "improved", "0.75"),
                docker_sandbox=FakeDockerSandbox(),
                observe=observe,
            )
            runner(
                "agent-trace",
                1,
                {"instruction": "x", "allowed_tools": ["Write"], "last_verified_state": {"metric": 0.5}},
            )

            evidence = router.calls[0]["evidence_pack"]
            started = [
                event for event in observe.recent_events(limit=5)
                if event["event_type"] == "worktree_experiment_started"
            ][0]
            self.assertEqual(evidence.get("trace_id"), started["trace_id"])
            self.assertEqual(evidence.get("root_trace_id"), started["root_trace_id"])

    def test_worktree_runner_can_gate_promotion_through_brain(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)

            router = FakeRouter()
            brain = FakeBrain()
            promoted: list[str] = []
            runner = GitWorktreeExperimentRunner(
                repo_root=repo,
                worktree_root=root / "worktrees",
                router=router,
                brain=brain,
                evaluator=lambda path, state, diff: ExperimentEvaluation(0.8, "improved", "0.8"),
                promotion_executor=lambda path, state, diff: promoted.append(path.name) or {"promoted": True},
            )
            record = runner(
                "agent-b",
                2,
                {
                    "instruction": "Prepare promotion",
                    "allowed_tools": ["Write"],
                    "last_verified_state": {"metric": 0.3},
                    "promote_on_improvement": True,
                },
            )

            self.assertEqual(record.status, "executed")
            self.assertEqual(promoted, ["exp-2"])
            self.assertEqual(brain.calls[0]["action"], "promote_agent-b")

    def test_worktree_runner_does_not_promote_without_critical_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)

            class BlockingBrain:
                def __init__(self) -> None:
                    self.calls: list[dict] = []

                def execute_critical_action(self, **kwargs) -> CriticalActionExecution:
                    self.calls.append(kwargs)
                    return CriticalActionExecution(
                        action=kwargs["action"],
                        status="awaiting_approval",
                        executed=False,
                        verification=CriticalActionVerification(
                            recommendation="needs_approval",
                            risk_level="critical",
                            summary="critical promote gate",
                            should_proceed=False,
                            requires_human_approval=True,
                        ),
                    )

            brain = BlockingBrain()
            promoted: list[str] = []
            runner = GitWorktreeExperimentRunner(
                repo_root=repo,
                worktree_root=root / "worktrees",
                router=FakeRouter(),
                brain=brain,
                evaluator=lambda path, state, diff: ExperimentEvaluation(0.8, "improved", "0.8"),
                promotion_executor=lambda path, state, diff: promoted.append(path.name) or {"promoted": True},
            )
            record = runner(
                "self-improve",
                1,
                {
                    "instruction": "Prepare promotion",
                    "allowed_tools": ["Write"],
                    "last_verified_state": {"metric": 0.3},
                    "promote_on_improvement": True,
                    "commit_on_promotion": True,
                },
            )

            self.assertEqual(record.status, "awaiting_approval")
            self.assertEqual(brain.calls[0]["action"], "promote_self-improve")
            self.assertEqual(promoted, [])

    def test_worktree_runner_falls_back_to_snapshot_when_repo_has_no_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")

            router = FakeRouter()
            runner = GitWorktreeExperimentRunner(
                repo_root=repo,
                worktree_root=root / "worktrees",
                router=router,
                evaluator=lambda path, state, diff: ExperimentEvaluation(0.6, "improved", diff),
            )
            record = runner(
                "agent-c",
                1,
                {"instruction": "Change the workspace", "allowed_tools": ["Write"], "last_verified_state": {"metric": 0.1}},
            )

            self.assertEqual(record.metric_value, 0.6)
            self.assertFalse((root / "worktrees" / "agent-c" / "exp-1").exists())

    def test_worktree_promotion_ignores_unrelated_dirty_repo_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")
            (repo / "dirty.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md", "dirty.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)
            (repo / "dirty.txt").write_text("local only\n", encoding="utf-8")

            class SnapshotRouter(FakeRouter):
                def ask(self, prompt: str, **kwargs) -> LLMResponse:
                    worktree = Path(kwargs["cwd"])
                    (worktree / "PROMOTED.txt").write_text("promoted\n", encoding="utf-8")
                    return LLMResponse(
                        content="Applied one incremental change.",
                        lane="worker",
                        provider="anthropic",
                        model="claude-sonnet-4-5",
                        cost_estimate=0.05,
                    )

            brain = FakeBrain()
            runner = GitWorktreeExperimentRunner(
                repo_root=repo,
                worktree_root=root / "worktrees",
                router=SnapshotRouter(),
                brain=brain,
                evaluator=lambda path, state, diff: ExperimentEvaluation(0.8, "improved", "0.8"),
                promotion_executor=WorkspacePromotionExecutor(repo),
            )
            record = runner(
                "agent-dirty",
                1,
                {
                    "instruction": "Add file for promotion",
                    "allowed_tools": ["Write"],
                    "last_verified_state": {"metric": 0.1},
                    "promote_on_improvement": True,
                },
            )

            self.assertEqual(record.status, "executed")
            self.assertEqual((repo / "PROMOTED.txt").read_text(encoding="utf-8"), "promoted\n")
            self.assertEqual((repo / "dirty.txt").read_text(encoding="utf-8"), "local only\n")

    def test_worktree_runner_with_real_promotion_executor_updates_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")

            class SnapshotRouter(FakeRouter):
                def ask(self, prompt: str, **kwargs) -> LLMResponse:
                    worktree = Path(kwargs["cwd"])
                    (worktree / "PROMOTED.txt").write_text("promoted\n", encoding="utf-8")
                    return LLMResponse(
                        content="Applied one incremental change.",
                        lane="worker",
                        provider="anthropic",
                        model="claude-sonnet-4-5",
                        cost_estimate=0.05,
                    )

            brain = FakeBrain()
            runner = GitWorktreeExperimentRunner(
                repo_root=repo,
                worktree_root=root / "worktrees",
                router=SnapshotRouter(),
                brain=brain,
                evaluator=lambda path, state, diff: ExperimentEvaluation(0.8, "improved", "0.8"),
                promotion_executor=WorkspacePromotionExecutor(repo),
            )
            record = runner(
                "agent-d",
                1,
                {
                    "instruction": "Add file for promotion",
                    "allowed_tools": ["Write"],
                    "last_verified_state": {"metric": 0.1},
                    "promote_on_improvement": True,
                },
            )

            self.assertEqual(record.status, "executed")
            self.assertEqual((repo / "PROMOTED.txt").read_text(encoding="utf-8"), "promoted\n")

    def test_git_commit_promotion_executor_commits_only_manifest_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            (repo / "dirty.txt").write_text("keep me dirty\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt", "dirty.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)
            (repo / "dirty.txt").write_text("local only\n", encoding="utf-8")

            worktree = root / "worktree"
            worktree.mkdir()
            (worktree / "tracked.txt").write_text("promoted\n", encoding="utf-8")
            (worktree / "added.txt").write_text("new file\n", encoding="utf-8")

            executor = GitCommitPromotionExecutor(repo)
            result = executor(
                worktree,
                {
                    "name": "publisher",
                    "commit_on_promotion": True,
                    "_workspace_mode": "snapshot",
                },
                "ADDED added.txt\nMODIFIED tracked.txt",
            )

            self.assertTrue(result.commit_created)
            self.assertEqual(result.commit_message, "chore(claw): promote publisher")
            self.assertIsNotNone(result.commit_sha)
            self.assertEqual((repo / "tracked.txt").read_text(encoding="utf-8"), "promoted\n")
            self.assertEqual((repo / "added.txt").read_text(encoding="utf-8"), "new file\n")

            show = subprocess.run(
                ["git", "show", "--name-only", "--format=%s", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            show = [line for line in show if line]
            self.assertEqual(show[0], "chore(claw): promote publisher")
            self.assertCountEqual(show[1:], ["added.txt", "tracked.txt"])

            status = subprocess.run(
                ["git", "status", "--short"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn(" M dirty.txt", status)
            self.assertNotIn("dirty.txt", show[1:])

    def test_git_commit_promotion_executor_skips_commit_without_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)

            worktree = root / "worktree"
            worktree.mkdir()
            (worktree / "tracked.txt").write_text("promoted\n", encoding="utf-8")

            executor = GitCommitPromotionExecutor(repo)
            result = executor(worktree, {"name": "publisher", "commit_on_promotion": False}, "diff")

            self.assertFalse(result.commit_created)
            self.assertIsNone(result.commit_sha)
            self.assertEqual(
                subprocess.run(
                    ["git", "rev-list", "--count", "HEAD"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                "1",
            )

    def test_git_branch_promotion_executor_creates_branch_without_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)
            current_branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            current_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            worktree = root / "worktree"
            worktree.mkdir()
            (worktree / "tracked.txt").write_text("promoted\n", encoding="utf-8")

            executor = GitBranchPromotionExecutor(repo)
            result = executor(
                worktree,
                {
                    "name": "publisher",
                    "commit_on_promotion": True,
                    "branch_on_promotion": True,
                    "_workspace_mode": "snapshot",
                },
                "MODIFIED tracked.txt",
            )

            self.assertTrue(result.commit_created)
            self.assertTrue(result.branch_created)
            self.assertTrue(result.branch_name.startswith("claw/publisher/"))
            self.assertEqual(
                subprocess.run(
                    ["git", "rev-parse", result.branch_name],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                result.commit_sha,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "branch", "--show-current"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                current_branch,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                current_head,
            )
            self.assertEqual((repo / "tracked.txt").read_text(encoding="utf-8"), "base\n")
            self.assertEqual(
                subprocess.run(
                    ["git", "show", f"{result.branch_name}:tracked.txt"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "promoted\n",
            )

    def test_git_branch_promotion_defaults_to_isolated_branch_when_commit_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)
            current_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            worktree = root / "worktree"
            worktree.mkdir()
            (worktree / "tracked.txt").write_text("promoted\n", encoding="utf-8")

            result = GitBranchPromotionExecutor(repo)(
                worktree,
                {
                    "name": "publisher",
                    "commit_on_promotion": True,
                    "_workspace_mode": "snapshot",
                },
                "MODIFIED tracked.txt",
            )

            self.assertTrue(result.commit_created)
            self.assertTrue(result.branch_created)
            self.assertIsNotNone(result.branch_name)
            self.assertEqual(
                subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                current_head,
            )
            self.assertEqual((repo / "tracked.txt").read_text(encoding="utf-8"), "base\n")
            self.assertEqual(
                subprocess.run(
                    ["git", "show", f"{result.branch_name}:tracked.txt"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "promoted\n",
            )

    def test_git_branch_promotion_ignores_live_head_state_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)
            current_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            worktree = root / "worktree"
            worktree.mkdir()
            (worktree / "tracked.txt").write_text("promoted\n", encoding="utf-8")

            result = GitBranchPromotionExecutor(repo)(
                worktree,
                {
                    "name": "self-improve",
                    "commit_on_promotion": True,
                    "branch_on_promotion": False,
                    "allow_live_head_promotion": True,
                    "_workspace_mode": "snapshot",
                },
                "MODIFIED tracked.txt",
            )

            self.assertTrue(result.commit_created)
            self.assertTrue(result.branch_created)
            self.assertEqual(
                subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                current_head,
            )
            self.assertEqual((repo / "tracked.txt").read_text(encoding="utf-8"), "base\n")
            self.assertEqual(
                subprocess.run(
                    ["git", "show", f"{result.branch_name}:tracked.txt"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "promoted\n",
            )

    def test_promotion_tooling_gate_runs_only_on_touched_python_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            worktree = Path(tmpdir)
            (worktree / "changed.py").write_text("print('ok')\n", encoding="utf-8")
            (worktree / "untouched_bad.py").write_text("this is not python\n", encoding="utf-8")
            runner = FakePromotionCommandRunner()
            gate = PromotionToolingGate(command_runner=runner)

            report = gate.evaluate(
                worktree_path=worktree,
                state={"name": "publisher", "_promotion_id": "promo-1"},
                manifest=PromotionManifest(modified=["changed.py"]),
                target_branch="claw/publisher/<commit_sha>",
            )

            self.assertEqual(report.decision, "passed")
            commands = [call["args"] for call in runner.calls]
            self.assertEqual(
                commands,
                [
                    ["uvx", "ruff", "check", "changed.py"],
                    ["uvx", "ruff", "format", "--check", "changed.py"],
                    ["uvx", "mypy", "changed.py"],
                ],
            )
            self.assertNotIn("untouched_bad.py", " ".join(" ".join(cmd) for cmd in commands))
            self.assertTrue(all("." not in cmd for cmd in commands))

    def test_promotion_tooling_gate_blocks_ruff_check_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            worktree = Path(tmpdir)
            (worktree / "changed.py").write_text("print('ok')\n", encoding="utf-8")
            gate = PromotionToolingGate(
                command_runner=FakePromotionCommandRunner(fail={"uvx ruff check": 1})
            )

            report = gate.evaluate(
                worktree_path=worktree,
                state={"name": "publisher", "_promotion_id": "promo-ruff"},
                manifest=PromotionManifest(modified=["changed.py"]),
                target_branch="claw/publisher/<commit_sha>",
            )

            self.assertEqual(report.decision, "failed")
            self.assertEqual(report.reason, "ruff_check_failed")
            self.assertEqual(report.ruff_check_status, "failed")
            self.assertEqual(report.ruff_format_status, "skipped_after_ruff_check_failed")
            self.assertEqual(report.mypy_status, "skipped_after_ruff_failed")

    def test_promotion_tooling_gate_blocks_ruff_format_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            worktree = Path(tmpdir)
            (worktree / "changed.py").write_text("print('ok')\n", encoding="utf-8")
            gate = PromotionToolingGate(
                command_runner=FakePromotionCommandRunner(fail={"uvx ruff format": 1})
            )

            report = gate.evaluate(
                worktree_path=worktree,
                state={"name": "publisher", "_promotion_id": "promo-format"},
                manifest=PromotionManifest(modified=["changed.py"]),
                target_branch="claw/publisher/<commit_sha>",
            )

            self.assertEqual(report.decision, "failed")
            self.assertEqual(report.reason, "ruff_format_failed")
            self.assertEqual(report.ruff_check_status, "passed")
            self.assertEqual(report.ruff_format_status, "failed")
            self.assertEqual(report.mypy_status, "skipped_after_ruff_failed")

    def test_promotion_tooling_gate_does_not_block_historical_baseline_ruff_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            baseline = root / "repo"
            worktree = root / "worktree"
            baseline.mkdir()
            worktree.mkdir()
            (baseline / "existing_bad.py").write_text("this is already bad\n", encoding="utf-8")
            (worktree / "existing_bad.py").write_text("this is still bad\n", encoding="utf-8")
            runner = FakePromotionCommandRunner(
                fail={"uvx ruff check existing_bad.py": 1, "uvx ruff format --check existing_bad.py": 1}
            )
            gate = PromotionToolingGate(baseline_root=baseline, command_runner=runner)

            report = gate.evaluate(
                worktree_path=worktree,
                state={"name": "publisher", "_promotion_id": "promo-baseline"},
                manifest=PromotionManifest(modified=["existing_bad.py"]),
                target_branch="claw/publisher/<commit_sha>",
            )

            self.assertEqual(report.decision, "passed")
            self.assertEqual(report.ruff_check_status, "passed_with_baseline_violations")
            self.assertEqual(report.ruff_format_status, "passed_with_baseline_violations")

    def test_promotion_tooling_gate_blocks_new_file_ruff_failure_even_when_baseline_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            baseline = root / "repo"
            worktree = root / "worktree"
            baseline.mkdir()
            worktree.mkdir()
            (baseline / "existing_bad.py").write_text("this is already bad\n", encoding="utf-8")
            (worktree / "existing_bad.py").write_text("this is still bad\n", encoding="utf-8")
            (worktree / "new_bad.py").write_text("new bad\n", encoding="utf-8")
            runner = FakePromotionCommandRunner(
                fail={
                    "uvx ruff check existing_bad.py": 1,
                    "uvx ruff check new_bad.py": 1,
                }
            )
            gate = PromotionToolingGate(baseline_root=baseline, command_runner=runner)

            report = gate.evaluate(
                worktree_path=worktree,
                state={"name": "publisher", "_promotion_id": "promo-new-bad"},
                manifest=PromotionManifest(modified=["existing_bad.py"], added=["new_bad.py"]),
                target_branch="claw/publisher/<commit_sha>",
            )

            self.assertEqual(report.decision, "failed")
            self.assertEqual(report.reason, "ruff_check_failed")
            self.assertEqual(report.ruff_check_status, "failed")

    def test_promotion_tooling_gate_mypy_failure_is_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            worktree = Path(tmpdir)
            (worktree / "changed.py").write_text("print('ok')\n", encoding="utf-8")
            gate = PromotionToolingGate(
                command_runner=FakePromotionCommandRunner(fail={"uvx mypy changed.py": 1})
            )

            report = gate.evaluate(
                worktree_path=worktree,
                state={"name": "publisher", "_promotion_id": "promo-mypy"},
                manifest=PromotionManifest(modified=["changed.py"]),
                target_branch="claw/publisher/<commit_sha>",
            )

            self.assertEqual(report.decision, "passed")
            self.assertEqual(report.reason, "passed")
            self.assertEqual(report.mypy_status, "advisory_failed")

    def test_promotion_tooling_gate_reports_sensitive_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            worktree = Path(tmpdir)
            sensitive = worktree / "claw_v2" / "brain.py"
            sensitive.parent.mkdir()
            sensitive.write_text("print('sensitive')\n", encoding="utf-8")
            gate = PromotionToolingGate(command_runner=FakePromotionCommandRunner())

            report = gate.evaluate(
                worktree_path=worktree,
                state={"name": "self-improve", "_promotion_id": "promo-sensitive"},
                manifest=PromotionManifest(modified=["claw_v2/brain.py"]),
                target_branch="claw/self-improve/<commit_sha>",
            )

            self.assertEqual(report.decision, "passed")
            self.assertEqual(report.sensitive_files_touched, ["claw_v2/brain.py"])

    def test_git_branch_promotion_blocks_ruff_failure_without_touching_live_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "changed.py").write_text("print('base')\n", encoding="utf-8")
            subprocess.run(["git", "add", "changed.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)
            current_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            worktree = root / "worktree"
            worktree.mkdir()
            (worktree / "changed.py").write_text("print('promoted')\n", encoding="utf-8")
            gate = PromotionToolingGate(
                command_runner=FakePromotionCommandRunner(fail={"uvx ruff check": 1})
            )

            with self.assertRaises(PromotionToolingError):
                GitBranchPromotionExecutor(repo, tooling_gate=gate)(
                    worktree,
                    {
                        "name": "publisher",
                        "commit_on_promotion": True,
                        "_workspace_mode": "snapshot",
                    },
                    "MODIFIED changed.py",
                )

            self.assertEqual(
                subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                current_head,
            )
            self.assertEqual((repo / "changed.py").read_text(encoding="utf-8"), "print('base')\n")
            branches = subprocess.run(
                ["git", "branch", "--list", "claw/*"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(branches.strip(), "")


if __name__ == "__main__":
    unittest.main()
