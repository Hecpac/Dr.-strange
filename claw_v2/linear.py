from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

McpCaller = Callable[..., Any]


@dataclass(slots=True)
class LinearIssue:
    id: str
    title: str
    description: str
    state: str
    labels: list[str]
    branch_name: str
    url: str


class LinearService:
    def __init__(self, mcp_caller: McpCaller) -> None:
        self._call = mcp_caller

    def list_actionable(self, label: str = "claw-auto", state: str = "Todo") -> list[LinearIssue]:
        raw = self._call("list_issues", label=label, state=state)
        if not isinstance(raw, list):
            return []
        return [self._parse_issue(item) for item in raw if self._matches(item, label, state)]

    def get_issue(self, issue_id: str) -> LinearIssue:
        raw = self._call("get_issue", id=issue_id)
        return self._parse_issue(raw)

    def update_status(self, issue_id: str, state: str) -> None:
        self._call("save_issue", id=issue_id, state=state)

    def post_comment(self, issue_id: str, body: str) -> None:
        self._call("save_comment", issueId=issue_id, body=body)

    def link_pr(self, issue_id: str, pr_url: str, pr_title: str) -> None:
        self._call("save_issue", id=issue_id, links=[{"url": pr_url, "title": pr_title}])

    @staticmethod
    def _parse_issue(raw: dict) -> LinearIssue:
        return LinearIssue(
            id=raw.get("identifier") or raw.get("id", ""),
            title=raw.get("title", ""),
            description=raw.get("description", ""),
            state=(raw.get("state") or {}).get("name", ""),
            labels=[label.get("name", "") for label in (raw.get("labels") or [])],
            branch_name=raw.get("branchName", ""),
            url=raw.get("url", ""),
        )

    @staticmethod
    def _matches(raw: dict, label: str, state: str) -> bool:
        issue_state = (raw.get("state") or {}).get("name", "")
        issue_labels = [l.get("name", "") for l in (raw.get("labels") or [])]
        return issue_state == state and label in issue_labels
