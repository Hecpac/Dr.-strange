from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from claw_v2.agents import (
    ExperimentEvaluation,
    GitBranchPromotionExecutor,
    GitCommitPromotionExecutor,
    GitWorktreeExperimentRunner,
    WorkspacePromotionExecutor,
)
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


if __name__ == "__main__":
    unittest.main()
