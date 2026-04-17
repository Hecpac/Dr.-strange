from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

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


_GRAPHQL_URL = "https://api.linear.app/graphql"

_QUERIES: dict[str, str] = {
    "list_issues": """
        query($label: String, $state: String) {
            issues(filter: {
                labels: { name: { eq: $label } }
                state: { name: { eq: $state } }
            }, first: 50) {
                nodes {
                    id identifier title description url
                    branchName
                    state { name }
                    labels { nodes { name } }
                }
            }
        }
    """,
    "get_issue": """
        query($id: String!) {
            issue(id: $id) {
                id identifier title description url
                branchName
                state { name }
                labels { nodes { name } }
            }
        }
    """,
}

_MUTATIONS: dict[str, str] = {
    "save_issue": """
        mutation($id: String!, $state: String, $links: [AttachmentCreateInput!]) {
            issueUpdate(id: $id, input: { stateId: $state }) { success }
        }
    """,
    "save_comment": """
        mutation($issueId: String!, $body: String!) {
            commentCreate(input: { issueId: $issueId, body: $body }) { success }
        }
    """,
}


def build_linear_api_caller(api_key: str) -> McpCaller:
    """Build a real Linear API caller using GraphQL over httpx."""
    import httpx

    client = httpx.Client(
        base_url=_GRAPHQL_URL,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        timeout=30,
    )

    def caller(action: str, **kwargs: Any) -> Any:
        if action == "list_issues":
            return _query_list_issues(client, kwargs.get("label"), kwargs.get("state"))
        if action == "get_issue":
            return _query_get_issue(client, kwargs["id"])
        if action == "save_issue":
            return _mutate_save_issue(client, kwargs)
        if action == "save_comment":
            return _mutate_save_comment(client, kwargs.get("issueId", ""), kwargs.get("body", ""))
        logger.warning("Unknown Linear action: %s", action)
        return None

    return caller


def _gql(client: Any, query: str, variables: dict) -> dict:
    response = client.post("", json={"query": query, "variables": variables})
    response.raise_for_status()
    data = response.json()
    if "errors" in data:
        logger.error("Linear GraphQL error: %s", data["errors"])
    return data.get("data") or {}


def _query_list_issues(client: Any, label: str | None, state: str | None) -> list[dict]:
    data = _gql(client, _QUERIES["list_issues"], {"label": label, "state": state})
    nodes = (data.get("issues") or {}).get("nodes") or []
    return [_normalize_node(n) for n in nodes]


def _query_get_issue(client: Any, issue_id: str) -> dict:
    data = _gql(client, _QUERIES["get_issue"], {"id": issue_id})
    node = data.get("issue")
    if node is None:
        raise FileNotFoundError(f"Issue not found: {issue_id}")
    return _normalize_node(node)


def _mutate_save_issue(client: Any, kwargs: dict) -> None:
    issue_id = kwargs.get("id", "")
    state = kwargs.get("state")
    links = kwargs.get("links")
    if state:
        _gql(client, """
            mutation($id: String!, $stateName: String!) {
                issueUpdate(id: $id, input: { stateName: $stateName }) { success }
            }
        """, {"id": issue_id, "stateName": state})
    if links:
        for link in links:
            _gql(client, """
                mutation($issueId: String!, $url: String!, $title: String!) {
                    attachmentCreate(input: { issueId: $issueId, url: $url, title: $title }) { success }
                }
            """, {"issueId": issue_id, "url": link["url"], "title": link["title"]})


def _mutate_save_comment(client: Any, issue_id: str, body: str) -> None:
    _gql(client, _MUTATIONS["save_comment"], {"issueId": issue_id, "body": body})


def _normalize_node(node: dict) -> dict:
    labels = node.get("labels") or {}
    label_nodes = labels.get("nodes") if isinstance(labels, dict) else labels
    return {
        **node,
        "labels": label_nodes or [],
    }
