from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.approval import ApprovalManager
from claw_v2.linear import LinearIssue, LinearService
from claw_v2.pipeline import PipelineRun, PipelineService


def _make_issue(issue_id: str = "HEC-1", title: str = "Fix bug") -> LinearIssue:
    return LinearIssue(
        id=issue_id, title=title, description="Fix the login bug",
        state="Todo", labels=["claw-auto"], branch_name=f"feat/{issue_id.lower()}-fix-bug",
        url=f"https://linear.app/issue/{issue_id}",
    )


class ProcessIssueTests(unittest.TestCase):
    def test_happy_path_reaches_awaiting_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".git").mkdir()
            linear = MagicMock(spec=LinearService)
            linear.get_issue.return_value = _make_issue()
            router = MagicMock()
            router.ask.return_value = MagicMock(content="implemented the fix", cost_estimate=0.01)
            approvals = ApprovalManager(root / "approvals", "secret")
            pr_svc = MagicMock()
            svc = PipelineService(
                linear=linear, router=router, approvals=approvals,
                pull_requests=pr_svc, observe=None,
                default_repo_root=root, max_retries=3,
                state_root=root / "pipeline",
            )
            with patch("claw_v2.pipeline._run_tests", return_value=(True, "5 passed")):
                with patch("claw_v2.pipeline._create_branch"):
                    with patch("claw_v2.pipeline._create_worktree", return_value=root / "wt"):
                        with patch("claw_v2.pipeline._collect_diff", return_value="diff content"):
                            with patch("claw_v2.pipeline._remove_worktree"):
                                run = svc.process_issue("HEC-1")
            self.assertEqual(run.status, "awaiting_approval")
            self.assertIsNotNone(run.approval_id)
            linear.update_status.assert_any_call("HEC-1", "In Progress")
            linear.post_comment.assert_called_once()

    def test_retries_on_test_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".git").mkdir()
            linear = MagicMock(spec=LinearService)
            linear.get_issue.return_value = _make_issue()
            router = MagicMock()
            router.ask.return_value = MagicMock(content="fix attempt", cost_estimate=0.01)
            approvals = ApprovalManager(root / "approvals", "secret")
            svc = PipelineService(
                linear=linear, router=router, approvals=approvals,
                pull_requests=MagicMock(), observe=None,
                default_repo_root=root, max_retries=3,
                state_root=root / "pipeline",
            )
            test_results = [(False, "FAILED"), (False, "FAILED"), (True, "5 passed")]
            call_idx = [0]

            def mock_tests(*args, **kwargs):
                result = test_results[call_idx[0]]
                call_idx[0] += 1
                return result

            with patch("claw_v2.pipeline._run_tests", side_effect=mock_tests):
                with patch("claw_v2.pipeline._create_branch"):
                    with patch("claw_v2.pipeline._create_worktree", return_value=root / "wt"):
                        with patch("claw_v2.pipeline._collect_diff", return_value="diff"):
                            with patch("claw_v2.pipeline._remove_worktree"):
                                run = svc.process_issue("HEC-1")
            self.assertEqual(run.status, "awaiting_approval")
            self.assertEqual(run.retries, 2)
            self.assertEqual(router.ask.call_count, 3)

    def test_fails_after_max_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".git").mkdir()
            linear = MagicMock(spec=LinearService)
            linear.get_issue.return_value = _make_issue()
            router = MagicMock()
            router.ask.return_value = MagicMock(content="fix", cost_estimate=0.01)
            approvals = ApprovalManager(root / "approvals", "secret")
            svc = PipelineService(
                linear=linear, router=router, approvals=approvals,
                pull_requests=MagicMock(), observe=None,
                default_repo_root=root, max_retries=2,
                state_root=root / "pipeline",
            )
            with patch("claw_v2.pipeline._run_tests", return_value=(False, "FAILED")):
                with patch("claw_v2.pipeline._create_branch"):
                    with patch("claw_v2.pipeline._create_worktree", return_value=root / "wt"):
                        with patch("claw_v2.pipeline._collect_diff", return_value="diff"):
                            with patch("claw_v2.pipeline._remove_worktree"):
                                run = svc.process_issue("HEC-1")
            self.assertEqual(run.status, "failed")
            linear.post_comment.assert_called_once()
            self.assertIn("failed", linear.post_comment.call_args[0][1].lower())


class CompletePipelineTests(unittest.TestCase):
    def test_creates_pr_after_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state_root = root / "pipeline"
            state_root.mkdir(parents=True)
            approvals = ApprovalManager(root / "approvals", "secret")
            pending = approvals.create(action="pipeline", summary="test")
            approvals.approve(pending.approval_id, pending.token)

            run_data = {
                "issue_id": "HEC-1", "branch_name": "feat/hec-1", "repo_root": str(root),
                "status": "awaiting_approval", "approval_id": pending.approval_id,
                "approval_token": pending.token, "diff": "some diff", "test_output": "5 passed",
            }
            (state_root / "HEC-1.json").write_text(json.dumps(run_data))

            linear = MagicMock(spec=LinearService)
            pr_svc = MagicMock()
            pr_svc.create_pull_request.return_value = MagicMock(url="https://github.com/pr/1", number=1)
            svc = PipelineService(
                linear=linear, router=MagicMock(), approvals=approvals,
                pull_requests=pr_svc, observe=None,
                default_repo_root=root, state_root=state_root,
            )
            result = svc.complete_pipeline("HEC-1")
            self.assertEqual(result.status, "pr_created")
            self.assertEqual(result.pr_url, "https://github.com/pr/1")
            pr_svc.create_pull_request.assert_called_once()
            linear.link_pr.assert_called_once()
            linear.update_status.assert_called_with("HEC-1", "In Review")


class StatePersistenceTests(unittest.TestCase):
    def test_save_and_load_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_root = Path(tmpdir)
            svc = PipelineService(
                linear=MagicMock(), router=MagicMock(), approvals=MagicMock(),
                pull_requests=MagicMock(), observe=None,
                default_repo_root=Path("/tmp"), state_root=state_root,
            )
            run = PipelineRun(
                issue_id="HEC-1", branch_name="feat/hec-1",
                repo_root="/tmp", status="awaiting_approval",
            )
            svc._save_run(run)
            loaded = svc._load_run("HEC-1")
            self.assertEqual(loaded.issue_id, "HEC-1")
            self.assertEqual(loaded.status, "awaiting_approval")

    def test_list_active_returns_non_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_root = Path(tmpdir)
            svc = PipelineService(
                linear=MagicMock(), router=MagicMock(), approvals=MagicMock(),
                pull_requests=MagicMock(), observe=None,
                default_repo_root=Path("/tmp"), state_root=state_root,
            )
            for status in ["awaiting_approval", "pr_created", "done", "failed"]:
                run = PipelineRun(issue_id=f"HEC-{status}", branch_name="b", repo_root="/tmp", status=status)
                svc._save_run(run)
            active = svc.list_active()
            ids = [r.issue_id for r in active]
            self.assertIn("HEC-awaiting_approval", ids)
            self.assertIn("HEC-pr_created", ids)
            self.assertNotIn("HEC-done", ids)
            self.assertNotIn("HEC-failed", ids)


class MergeAndCloseTests(unittest.TestCase):
    def test_merge_and_close_happy_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_root = Path(tmpdir) / "pipeline"
            state_root.mkdir(parents=True)
            root = Path(tmpdir)

            run_data = {
                "issue_id": "HEC-1", "branch_name": "feat/hec-1", "repo_root": str(root),
                "status": "pr_created", "pr_url": "https://github.com/owner/repo/pull/42",
                "diff": "some diff", "test_output": "5 passed",
            }
            (state_root / "HEC-1.json").write_text(json.dumps(run_data))

            linear = MagicMock(spec=LinearService)
            pr_svc = MagicMock()
            pr_svc.merge_pull_request.return_value = "merged"
            svc = PipelineService(
                linear=linear, router=MagicMock(), approvals=MagicMock(),
                pull_requests=pr_svc, observe=None,
                default_repo_root=root, state_root=state_root,
            )
            result = svc.merge_and_close("HEC-1")
            self.assertEqual(result.status, "done")
            pr_svc.merge_pull_request.assert_called_once_with(42)
            linear.update_status.assert_called_with("HEC-1", "Done")
            linear.post_comment.assert_called_once()

    def test_merge_skips_non_pr_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_root = Path(tmpdir) / "pipeline"
            state_root.mkdir(parents=True)
            run_data = {
                "issue_id": "HEC-2", "branch_name": "feat/hec-2",
                "repo_root": str(tmpdir), "status": "awaiting_approval",
            }
            (state_root / "HEC-2.json").write_text(json.dumps(run_data))
            svc = PipelineService(
                linear=MagicMock(), router=MagicMock(), approvals=MagicMock(),
                pull_requests=MagicMock(), observe=None,
                default_repo_root=Path(tmpdir), state_root=state_root,
            )
            result = svc.merge_and_close("HEC-2")
            self.assertEqual(result.status, "awaiting_approval")


class PollMergesTests(unittest.TestCase):
    def test_detects_externally_merged_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_root = Path(tmpdir) / "pipeline"
            state_root.mkdir(parents=True)
            run_data = {
                "issue_id": "HEC-3", "branch_name": "feat/hec-3",
                "repo_root": str(tmpdir), "status": "pr_created",
                "pr_url": "https://github.com/owner/repo/pull/99",
            }
            (state_root / "HEC-3.json").write_text(json.dumps(run_data))
            linear = MagicMock(spec=LinearService)
            pr_svc = MagicMock()
            pr_svc.get_pr_state.return_value = "MERGED"
            svc = PipelineService(
                linear=linear, router=MagicMock(), approvals=MagicMock(),
                pull_requests=pr_svc, observe=None,
                default_repo_root=Path(tmpdir), state_root=state_root,
            )
            closed = svc.poll_merges()
            self.assertEqual(len(closed), 1)
            self.assertEqual(closed[0].status, "done")
            linear.update_status.assert_called_with("HEC-3", "Done")

    def test_ignores_open_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_root = Path(tmpdir) / "pipeline"
            state_root.mkdir(parents=True)
            run_data = {
                "issue_id": "HEC-4", "branch_name": "feat/hec-4",
                "repo_root": str(tmpdir), "status": "pr_created",
                "pr_url": "https://github.com/owner/repo/pull/100",
            }
            (state_root / "HEC-4.json").write_text(json.dumps(run_data))
            pr_svc = MagicMock()
            pr_svc.get_pr_state.return_value = "OPEN"
            svc = PipelineService(
                linear=MagicMock(), router=MagicMock(), approvals=MagicMock(),
                pull_requests=pr_svc, observe=None,
                default_repo_root=Path(tmpdir), state_root=state_root,
            )
            closed = svc.poll_merges()
            self.assertEqual(len(closed), 0)


if __name__ == "__main__":
    unittest.main()
