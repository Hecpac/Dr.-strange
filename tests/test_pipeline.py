from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.approval import ApprovalManager
from claw_v2.linear import LinearIssue, LinearService
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.pipeline import (
    PipelineRun,
    PipelineService,
    _create_branch,
    _create_worktree,
    _derive_lesson,
    _push_branch,
    _record_outcome,
    _retrieve_lessons,
    _validate_branch_name,
)
from claw_v2.types import LLMResponse


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

    def test_process_issue_rejects_unsafe_branch_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".git").mkdir()
            linear = MagicMock(spec=LinearService)
            issue = _make_issue()
            issue.branch_name = "-bad-branch"
            linear.get_issue.return_value = issue
            svc = PipelineService(
                linear=linear,
                router=MagicMock(),
                approvals=ApprovalManager(root / "approvals", "secret"),
                pull_requests=MagicMock(),
                observe=None,
                default_repo_root=root,
                max_retries=2,
                state_root=root / "pipeline",
            )
            with self.assertRaises(ValueError):
                svc.process_issue("HEC-1")


class BranchValidationTests(unittest.TestCase):
    def test_validate_branch_name_rejects_git_unsafe_forms(self) -> None:
        for branch in ("-bad", "feat//oops", "feat/oops.lock", "feat/@{bad}", "feat\\oops", "feat/end/"):
            with self.subTest(branch=branch):
                with self.assertRaises(ValueError):
                    _validate_branch_name(branch)

    def test_git_commands_terminate_options_before_branch_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("claw_v2.pipeline.subprocess.run") as mock_run:
                _create_branch(root, "feat/safe")
                _create_worktree(root, "feat/safe")
                _push_branch(root, "feat/safe")

            branch_cmd = mock_run.call_args_list[0].args[0]
            worktree_cmd = mock_run.call_args_list[1].args[0]
            push_cmd = mock_run.call_args_list[2].args[0]
            self.assertIn("--", branch_cmd)
            self.assertIn("--", worktree_cmd)
            self.assertIn("--", push_cmd)


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
    def test_poll_actionable_degrades_and_backs_off_on_linear_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            now = [1000.0]
            linear = MagicMock(spec=LinearService)
            linear.list_actionable.side_effect = TimeoutError("The read operation timed out")
            observe = MagicMock()
            svc = PipelineService(
                linear=linear,
                router=MagicMock(),
                approvals=MagicMock(),
                pull_requests=MagicMock(),
                observe=observe,
                default_repo_root=Path(tmpdir),
                state_root=Path(tmpdir) / "pipeline",
                clock=lambda: now[0],
            )

            first = svc.poll_actionable()
            second = svc.poll_actionable()

            self.assertEqual(first, [])
            self.assertEqual(second, [])
            self.assertEqual(linear.list_actionable.call_count, 1)
            observe.emit.assert_any_call(
                "pipeline_poll_degraded",
                payload={
                    "poller": "pipeline_poll",
                    "reason": "timeout",
                    "error": "The read operation timed out",
                    "consecutive_failures": 1,
                    "backoff_seconds": 300.0,
                },
            )
            skipped_events = [
                call for call in observe.emit.call_args_list
                if call.args and call.args[0] == "pipeline_poll_skipped"
            ]
            self.assertEqual(len(skipped_events), 1)
            self.assertEqual(skipped_events[0].kwargs["payload"]["reason"], "linear_backoff")

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


class LearningLoopTests(unittest.TestCase):
    def test_record_and_retrieve_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mem = MemoryStore(Path(tmpdir) / "test.db")
            issue = _make_issue()
            run = PipelineRun(
                issue_id="HEC-1", branch_name="feat/hec-1",
                repo_root="/tmp", status="failed",
                test_output="AssertionError: expected 5 got 3", retries=3,
            )
            _record_outcome(mem, issue, run, "failure")
            results = mem.search_past_outcomes("login", task_type="pipeline")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["outcome"], "failure")
            self.assertIn("Assertion", results[0]["lesson"])

    def test_retrieve_lessons_formats_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mem = MemoryStore(Path(tmpdir) / "test.db")
            mem.store_task_outcome(
                task_type="pipeline", task_id="HEC-0",
                description="Fix login bug",
                approach="branch=feat/hec-0", outcome="failure",
                lesson="Check auth middleware first.",
                error_snippet="401 Unauthorized",
            )
            lessons = _retrieve_lessons(mem, "Fix the login bug")
            self.assertIn("FAILED", lessons)
            self.assertIn("Check auth middleware", lessons)
            self.assertIn("401", lessons)

    def test_retrieve_lessons_empty_when_no_memory(self) -> None:
        self.assertEqual(_retrieve_lessons(None, "anything"), "")

    def test_derive_lesson_success_first_try(self) -> None:
        run = PipelineRun(issue_id="X", branch_name="b", repo_root="/tmp", status="done", retries=0)
        self.assertIn("first attempt", _derive_lesson(run, "success"))

    def test_derive_lesson_import_error(self) -> None:
        run = PipelineRun(
            issue_id="X", branch_name="b", repo_root="/tmp", status="failed",
            test_output="ModuleNotFoundError: No module named 'foo'\nimport error", retries=2,
        )
        self.assertIn("Import", _derive_lesson(run, "failure"))

    def test_recent_failures_returns_only_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mem = MemoryStore(Path(tmpdir) / "test.db")
            mem.store_task_outcome(
                task_type="pipeline", task_id="A", description="d", approach="a",
                outcome="success", lesson="ok",
            )
            mem.store_task_outcome(
                task_type="pipeline", task_id="B", description="d", approach="a",
                outcome="failure", lesson="bad", error_snippet="err",
            )
            failures = mem.recent_failures(task_type="pipeline")
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["task_id"], "B")

    def test_negative_feedback_records_negative_preference(self) -> None:
        from claw_v2.learning import LearningLoop

        with tempfile.TemporaryDirectory() as tmpdir:
            mem = MemoryStore(Path(tmpdir) / "test.db")
            loop = LearningLoop(memory=mem)
            outcome_id = loop.record(
                task_type="coding",
                task_id="task-1",
                description="Implement feature",
                approach="use library X",
                outcome="failure",
                lesson="Avoid library X.",
            )

            loop.feedback(outcome_id, "negative: no uses esa libreria")

            facts = mem.search_facts("negative_preference")
            self.assertEqual(len(facts), 1)
            self.assertIn("Avoid approach 'use library X'", facts[0]["value"])
            self.assertIn("no uses esa libreria", facts[0]["value"])

    def test_consolidate_passes_evidence_pack_to_judge_lane(self) -> None:
        from claw_v2.learning import LearningLoop

        with tempfile.TemporaryDirectory() as tmpdir:
            mem = MemoryStore(Path(tmpdir) / "test.db")
            router = MagicMock()
            router.ask.return_value = LLMResponse(
                content="Switch tools after repeated failures.",
                lane="judge",
                provider="anthropic",
                model="test",
            )
            for index in range(10):
                mem.store_task_outcome(
                    task_type="coding",
                    task_id=f"task-{index}",
                    description=f"Task {index}",
                    approach="retry same command",
                    outcome="failure",
                    lesson="Switch tools after repeated failures.",
                )

            loop = LearningLoop(memory=mem, router=router)
            rules = loop.consolidate(min_outcomes=10)

            self.assertEqual(rules, "Switch tools after repeated failures.")
            evidence_pack = router.ask.call_args.kwargs["evidence_pack"]
            self.assertEqual(evidence_pack["operation"], "learning_consolidate")
            self.assertEqual(evidence_pack["outcome_count"], 10)

    def test_suggest_soul_updates_stores_reviewable_proposal(self) -> None:
        from claw_v2.learning import LearningLoop

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mem = MemoryStore(root / "test.db")
            observe = ObserveStream(root / "test.db")
            router = MagicMock()
            router.ask.return_value = LLMResponse(
                content=json.dumps(
                    {
                        "summary": "Failures show repeated strategy drift.",
                        "suggestions": [
                            {
                                "section": "Autonomy",
                                "change": "Before repeating a failed strategy, switch tools and summarize evidence.",
                                "reason": "Recent failures show repeated retries without changing approach.",
                                "priority": "high",
                                "evidence": ["failure with three retries"],
                            }
                        ],
                        "do_not_change": ["Keep approval boundaries intact."],
                    }
                ),
                lane="judge",
                provider="anthropic",
                model="test",
            )
            loop = LearningLoop(memory=mem, router=router)
            mem.store_task_outcome(
                task_type="coding",
                task_id="task-1",
                description="Fix deploy bug",
                approach="retry same command",
                outcome="failure",
                lesson="Switch tools after repeated failure.",
                error_snippet="timeout",
                retries=3,
            )
            observe.emit("llm_decision", payload={"api_key": "sk-secret", "status": "failed"})
            observe.emit("kairos_notify_suppressed", payload={"message": "routine"})

            proposal = loop.suggest_soul_updates(observe=observe, soul_text="# Claw\nSecurity Boundaries")

            self.assertIsNotNone(proposal)
            assert proposal is not None
            self.assertEqual(proposal["suggestions"][0]["priority"], "high")
            facts = mem.search_facts("soul_update_suggestion")
            self.assertEqual(len(facts), 1)
            self.assertIn("Before repeating a failed strategy", facts[0]["value"])
            events = observe.recent_events(limit=5)
            self.assertTrue(any(event["event_type"] == "soul_update_suggestion" for event in events))
            prompt = router.ask.call_args.args[0]
            self.assertIn("<redacted>", prompt)
            self.assertNotIn("sk-secret", prompt)
            evidence_pack = router.ask.call_args.kwargs["evidence_pack"]
            self.assertEqual(evidence_pack["operation"], "soul_update_proposal")
            self.assertGreaterEqual(evidence_pack["signal_count"], 1)

    def test_pipeline_injects_lessons(self) -> None:
        """Integration: process_issue should pass past lessons to the LLM prompt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".git").mkdir()
            mem = MemoryStore(root / "test.db")
            mem.store_task_outcome(
                task_type="pipeline", task_id="HEC-0",
                description="Fix the login bug",
                approach="branch=feat/hec-0", outcome="failure",
                lesson="Always validate JWT before checking permissions.",
                error_snippet="401 Unauthorized",
            )
            linear = MagicMock(spec=LinearService)
            linear.get_issue.return_value = _make_issue()
            router = MagicMock()
            router.ask.return_value = MagicMock(content="fix", cost_estimate=0.01)
            approvals = ApprovalManager(root / "approvals", "secret")
            svc = PipelineService(
                linear=linear, router=router, approvals=approvals,
                pull_requests=MagicMock(), observe=None,
                default_repo_root=root, max_retries=3,
                state_root=root / "pipeline", memory=mem,
            )
            with patch("claw_v2.pipeline._run_tests", return_value=(True, "5 passed")):
                with patch("claw_v2.pipeline._create_branch"):
                    with patch("claw_v2.pipeline._create_worktree", return_value=root / "wt"):
                        with patch("claw_v2.pipeline._collect_diff", return_value="diff"):
                            with patch("claw_v2.pipeline._remove_worktree"):
                                svc.process_issue("HEC-1")
            prompt_sent = router.ask.call_args[0][0]
            self.assertIn("Lessons from similar past tasks", prompt_sent)
            self.assertIn("JWT", prompt_sent)


if __name__ == "__main__":
    unittest.main()
