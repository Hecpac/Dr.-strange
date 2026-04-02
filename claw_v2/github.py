from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


CommandRunner = Callable[[Sequence[str]], str]


@dataclass(slots=True)
class PullRequestResult:
    url: str
    branch_name: str
    title: str
    base_branch: str | None = None
    number: int | None = None
    draft: bool = True


class GitHubPullRequestService:
    def __init__(
        self,
        repo_root: Path | str,
        *,
        remote_name: str = "origin",
        gh_path: str = "gh",
        git_path: str = "git",
        command_runner: CommandRunner | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.remote_name = remote_name
        self.gh_path = gh_path
        self.git_path = git_path
        self.command_runner = command_runner or self._run_command

    def create_pull_request(
        self,
        *,
        branch_name: str,
        title: str,
        body: str,
        base_branch: str | None = None,
        draft: bool = True,
    ) -> PullRequestResult:
        self.command_runner([self.git_path, "-C", str(self.repo_root), "push", "-u", self.remote_name, branch_name])

        command = [
            self.gh_path,
            "pr",
            "create",
            "--head",
            branch_name,
            "--title",
            title,
            "--body",
            body,
        ]
        if draft:
            command.append("--draft")
        if base_branch:
            command.extend(["--base", base_branch])

        output = self.command_runner(command).strip()
        number = _parse_pr_number(output)
        return PullRequestResult(
            url=output,
            branch_name=branch_name,
            title=title,
            base_branch=base_branch,
            number=number,
            draft=draft,
        )

    def merge_pull_request(self, pr_number: int) -> str:
        """Merge a PR by number. Returns merge status string."""
        output = self.command_runner([
            self.gh_path, "pr", "merge", str(pr_number),
            "--merge", "--repo", self._repo_nwo(),
        ])
        return output

    def get_pr_state(self, pr_number: int) -> str:
        """Return PR state: 'OPEN', 'CLOSED', or 'MERGED'."""
        output = self.command_runner([
            self.gh_path, "pr", "view", str(pr_number),
            "--json", "state", "--repo", self._repo_nwo(),
        ])
        data = json.loads(output)
        return data.get("state", "UNKNOWN")

    def _repo_nwo(self) -> str:
        """Get owner/repo from git remote."""
        output = self.command_runner([
            self.git_path, "-C", str(self.repo_root),
            "remote", "get-url", self.remote_name,
        ])
        return _parse_nwo(output)

    @staticmethod
    def _run_command(command: Sequence[str]) -> str:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip()


def _parse_pr_number(url: str) -> int | None:
    match = re.search(r"/pull/(\d+)$", url)
    if match is None:
        return None
    return int(match.group(1))


def _parse_nwo(remote_url: str) -> str:
    """Extract owner/repo from a git remote URL."""
    remote_url = remote_url.strip().rstrip(".git")
    match = re.search(r"[/:]([^/:]+/[^/:]+)$", remote_url)
    if match:
        return match.group(1)
    return remote_url
