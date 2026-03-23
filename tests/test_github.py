from __future__ import annotations

import unittest
from pathlib import Path

from claw_v2.github import GitHubPullRequestService, _parse_pr_number


class GitHubPullRequestServiceTests(unittest.TestCase):
    def test_parse_pr_number(self) -> None:
        self.assertEqual(_parse_pr_number("https://github.com/acme/repo/pull/42"), 42)
        self.assertIsNone(_parse_pr_number("not-a-pr-url"))

    def test_create_pull_request_pushes_branch_and_returns_metadata(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> str:
            commands.append(command)
            if command[:3] == ["git", "-C", "/tmp/repo"]:
                return ""
            return "https://github.com/acme/repo/pull/42"

        service = GitHubPullRequestService(
            Path("/tmp/repo"),
            command_runner=runner,
        )
        result = service.create_pull_request(
            branch_name="claw/agent/abc1234",
            title="chore(claw): publish agent",
            body="body",
            draft=True,
        )

        self.assertEqual(commands[0], ["git", "-C", "/tmp/repo", "push", "-u", "origin", "claw/agent/abc1234"])
        self.assertEqual(
            commands[1],
            [
                "gh",
                "pr",
                "create",
                "--head",
                "claw/agent/abc1234",
                "--title",
                "chore(claw): publish agent",
                "--body",
                "body",
                "--draft",
            ],
        )
        self.assertEqual(result.url, "https://github.com/acme/repo/pull/42")
        self.assertEqual(result.number, 42)


if __name__ == "__main__":
    unittest.main()
