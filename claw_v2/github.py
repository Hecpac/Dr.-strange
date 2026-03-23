from __future__ import annotations

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
