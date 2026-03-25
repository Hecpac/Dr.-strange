from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from claw_v2.linear import LinearService, LinearIssue


class ListActionableTests(unittest.TestCase):
    def test_filters_by_label_and_state(self) -> None:
        caller = MagicMock()
        caller.return_value = [
            {"id": "abc", "identifier": "HEC-1", "title": "Fix bug", "description": "details",
             "state": {"name": "Todo"}, "labels": [{"name": "claw-auto"}],
             "branchName": "feat/hec-1-fix-bug", "url": "https://linear.app/hec/issue/HEC-1"},
            {"id": "def", "identifier": "HEC-2", "title": "Other", "description": "",
             "state": {"name": "Done"}, "labels": [{"name": "claw-auto"}],
             "branchName": "feat/hec-2-other", "url": "https://linear.app/hec/issue/HEC-2"},
        ]
        svc = LinearService(caller)
        result = svc.list_actionable(label="claw-auto", state="Todo")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "HEC-1")

    def test_returns_empty_when_no_matches(self) -> None:
        caller = MagicMock(return_value=[])
        svc = LinearService(caller)
        self.assertEqual(svc.list_actionable(), [])


class GetIssueTests(unittest.TestCase):
    def test_parses_issue_data(self) -> None:
        caller = MagicMock(return_value={
            "id": "abc", "identifier": "HEC-5", "title": "Add feature",
            "description": "Build the thing", "state": {"name": "Todo"},
            "labels": [{"name": "claw-auto"}, {"name": "backend"}],
            "branchName": "feat/hec-5-add-feature",
            "url": "https://linear.app/hec/issue/HEC-5",
        })
        svc = LinearService(caller)
        issue = svc.get_issue("HEC-5")
        self.assertEqual(issue.id, "HEC-5")
        self.assertEqual(issue.title, "Add feature")
        self.assertEqual(issue.branch_name, "feat/hec-5-add-feature")
        self.assertEqual(issue.labels, ["claw-auto", "backend"])


class UpdateStatusTests(unittest.TestCase):
    def test_calls_mcp_with_state(self) -> None:
        caller = MagicMock()
        svc = LinearService(caller)
        svc.update_status("HEC-5", "In Progress")
        caller.assert_called_once_with("save_issue", id="HEC-5", state="In Progress")


class PostCommentTests(unittest.TestCase):
    def test_calls_mcp_with_body(self) -> None:
        caller = MagicMock()
        svc = LinearService(caller)
        svc.post_comment("HEC-5", "Pipeline started")
        caller.assert_called_once_with("save_comment", issueId="HEC-5", body="Pipeline started")


class LinkPrTests(unittest.TestCase):
    def test_calls_mcp_with_link(self) -> None:
        caller = MagicMock()
        svc = LinearService(caller)
        svc.link_pr("HEC-5", "https://github.com/org/repo/pull/1", "feat: add feature")
        caller.assert_called_once()
        args = caller.call_args
        self.assertEqual(args.kwargs["id"], "HEC-5")
        self.assertIn({"url": "https://github.com/org/repo/pull/1", "title": "feat: add feature"}, args.kwargs["links"])


if __name__ == "__main__":
    unittest.main()
