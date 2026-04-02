from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from claw_v2.approval import ApprovalManager
from claw_v2.github import GitHubPullRequestService
from claw_v2.linear import LinearIssue, LinearService
from claw_v2.llm import LLMRouter
from claw_v2.observe import ObserveStream

TERMINAL_STATUSES = frozenset({"done", "failed"})


@dataclass(slots=True)
class PipelineRun:
    issue_id: str
    branch_name: str
    repo_root: str
    status: str
    worktree_path: str | None = None
    diff: str | None = None
    test_output: str | None = None
    pr_url: str | None = None
    approval_id: str | None = None
    approval_token: str | None = None
    retries: int = 0


class PipelineService:
    def __init__(
        self,
        linear: LinearService,
        router: LLMRouter,
        approvals: ApprovalManager,
        pull_requests: GitHubPullRequestService,
        observe: ObserveStream | None,
        default_repo_root: Path,
        max_retries: int = 3,
        state_root: Path | None = None,
    ) -> None:
        self.linear = linear
        self.router = router
        self.approvals = approvals
        self.pull_requests = pull_requests
        self.observe = observe
        self.default_repo_root = default_repo_root
        self.max_retries = max_retries
        self.state_root = state_root or (Path.home() / ".claw" / "pipeline")
        self.state_root.mkdir(parents=True, exist_ok=True)

    def process_issue(self, issue_id: str, *, repo_root: Path | None = None) -> PipelineRun:
        repo = repo_root or self.default_repo_root
        issue = self.linear.get_issue(issue_id)
        branch = issue.branch_name or _slugify_branch(issue.id, issue.title)
        self.linear.update_status(issue_id, "In Progress")
        run = PipelineRun(issue_id=issue_id, branch_name=branch, repo_root=str(repo), status="in_progress")
        _create_branch(repo, branch)
        wt_path = _create_worktree(repo, branch)
        run.worktree_path = str(wt_path)
        try:
            for attempt in range(self.max_retries + 1):
                prompt = _build_code_prompt(issue, run)
                response = self.router.ask(
                    prompt, lane="worker", system_prompt="You are a coding agent. Implement the requested change.",
                    evidence_pack={"issue": issue.id, "description": issue.description},
                    cwd=str(wt_path),
                )
                run.diff = _collect_diff(wt_path)
                passed, output = _run_tests(wt_path)
                run.test_output = output
                if passed:
                    run.status = "awaiting_approval"
                    break
                run.retries += 1
                if run.retries >= self.max_retries:
                    run.status = "failed"
                    self.linear.post_comment(issue_id, f"Pipeline failed after {self.max_retries} retries.\n\n```\n{output[:1000]}\n```")
                    self._save_run(run)
                    return run
            if run.status == "awaiting_approval":
                pending = self.approvals.create(
                    action=f"pipeline:{issue_id}",
                    summary=f"Pipeline for {issue_id}: {issue.title}",
                )
                run.approval_id = pending.approval_id
                run.approval_token = pending.token
                summary = f"Pipeline ready for {issue_id}.\n\n**Changes:**\n```\n{(run.diff or '')[:500]}\n```\n\n**Tests:** {run.test_output[:200] if run.test_output else 'passed'}\n\nApprove via: `/pipeline_approve {pending.approval_id} {pending.token}`"
                self.linear.post_comment(issue_id, summary)
        finally:
            self._save_run(run)
        if self.observe:
            self.observe.emit("pipeline_checkpoint", payload={"issue": issue_id, "status": run.status})
        return run

    def complete_pipeline(self, issue_id: str) -> PipelineRun:
        run = self._load_run(issue_id)
        if run.approval_id:
            status = self.approvals.status(run.approval_id)
            if status != "approved":
                run.status = "blocked"
                self._save_run(run)
                return run
        repo = Path(run.repo_root)
        wt_path = Path(run.worktree_path) if run.worktree_path else None
        if wt_path and wt_path.exists():
            _commit_worktree(wt_path, f"feat: {issue_id} pipeline implementation")
            _push_branch(repo, run.branch_name)
        pr_result = self.pull_requests.create_pull_request(
            branch_name=run.branch_name,
            title=f"feat: {issue_id}",
            body=f"Automated PR from Claw pipeline.\n\nLinear: {issue_id}\n\nChanges:\n```\n{(run.diff or '')[:1000]}\n```",
            draft=False,
        )
        run.pr_url = pr_result.url
        run.status = "pr_created"
        self.linear.link_pr(issue_id, pr_result.url, pr_result.title)
        self.linear.update_status(issue_id, "In Review")
        if wt_path:
            _remove_worktree(repo, wt_path)
        self._save_run(run)
        return run

    def merge_and_close(self, issue_id: str) -> PipelineRun:
        """Merge the PR for a pipeline run and close the Linear issue."""
        run = self._load_run(issue_id)
        if run.status != "pr_created":
            return run
        if not run.pr_url:
            run.status = "failed"
            self._save_run(run)
            return run
        pr_number = _parse_pr_number_from_url(run.pr_url)
        if pr_number is None:
            run.status = "failed"
            self._save_run(run)
            return run
        self.pull_requests.merge_pull_request(pr_number)
        run.status = "merged"
        self._save_run(run)
        self.linear.update_status(issue_id, "Done")
        self.linear.post_comment(issue_id, f"PR merged and issue closed by Claw pipeline.\n\nPR: {run.pr_url}")
        run.status = "done"
        self._save_run(run)
        if self.observe:
            self.observe.emit("pipeline_done", payload={"issue": issue_id, "pr_url": run.pr_url})
        return run

    def poll_merges(self) -> list[PipelineRun]:
        """Check all pr_created runs and close those whose PRs have been merged externally."""
        closed: list[PipelineRun] = []
        for path in sorted(self.state_root.glob("*.json")):
            run = self._load_run_from_path(path)
            if run.status != "pr_created" or not run.pr_url:
                continue
            pr_number = _parse_pr_number_from_url(run.pr_url)
            if pr_number is None:
                continue
            state = self.pull_requests.get_pr_state(pr_number)
            if state == "MERGED":
                run.status = "done"
                self.linear.update_status(run.issue_id, "Done")
                self._save_run(run)
                closed.append(run)
        return closed

    def poll_actionable(self) -> list[PipelineRun]:
        issues = self.linear.list_actionable()
        runs: list[PipelineRun] = []
        for issue in issues:
            existing = self._try_load_run(issue.id)
            if existing and existing.status not in TERMINAL_STATUSES:
                continue
            run = self.process_issue(issue.id)
            runs.append(run)
        return runs

    def list_active(self) -> list[PipelineRun]:
        active: list[PipelineRun] = []
        for path in sorted(self.state_root.glob("*.json")):
            run = self._load_run_from_path(path)
            if run.status not in TERMINAL_STATUSES:
                active.append(run)
        return active

    def _save_run(self, run: PipelineRun) -> None:
        path = self.state_root / f"{run.issue_id}.json"
        path.write_text(json.dumps(asdict(run), indent=2), encoding="utf-8")

    def _load_run(self, issue_id: str) -> PipelineRun:
        path = self.state_root / f"{issue_id}.json"
        return self._load_run_from_path(path)

    def _try_load_run(self, issue_id: str) -> PipelineRun | None:
        path = self.state_root / f"{issue_id}.json"
        if not path.exists():
            return None
        return self._load_run_from_path(path)

    @staticmethod
    def _load_run_from_path(path: Path) -> PipelineRun:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PipelineRun(**{k: v for k, v in data.items() if k in PipelineRun.__dataclass_fields__})


def _slugify_branch(issue_id: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
    return f"feat/{issue_id.lower()}-{slug}"


def _build_code_prompt(issue: LinearIssue, run: PipelineRun) -> str:
    parts = [f"Implement the following issue: {issue.id} — {issue.title}", "", issue.description]
    if run.retries > 0 and run.test_output:
        parts.extend(["", f"Previous attempt failed. Test output:", f"```\n{run.test_output[:2000]}\n```", "Fix the failing tests."])
    return "\n".join(parts)


def _create_branch(repo: Path, branch: str) -> None:
    subprocess.run(["git", "-C", str(repo), "branch", "--no-track", branch, "HEAD"], capture_output=True, text=True, check=False)


def _create_worktree(repo: Path, branch: str) -> Path:
    wt_path = repo.parent / ".claw-worktrees" / branch.replace("/", "-")
    if wt_path.exists():
        shutil.rmtree(wt_path)
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", str(wt_path), branch], capture_output=True, text=True, check=True)
    return wt_path


def _remove_worktree(repo: Path, wt_path: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt_path)], capture_output=True, text=True, check=False)
    if wt_path.exists():
        shutil.rmtree(wt_path, ignore_errors=True)


def _collect_diff(wt_path: Path) -> str:
    result = subprocess.run(["git", "-C", str(wt_path), "diff", "--", "."], capture_output=True, text=True, check=False)
    status = subprocess.run(["git", "-C", str(wt_path), "status", "--porcelain"], capture_output=True, text=True, check=False)
    return (result.stdout or "") + "\n" + (status.stdout or "")


def _run_tests(wt_path: Path) -> tuple[bool, str]:
    result = subprocess.run(["python", "-m", "pytest", "-x", "-q"], cwd=str(wt_path), capture_output=True, text=True, check=False)
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output


def _commit_worktree(wt_path: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(wt_path), "add", "-A"], capture_output=True, text=True, check=True)
    subprocess.run(["git", "-C", str(wt_path), "commit", "-m", message, "--allow-empty"], capture_output=True, text=True, check=False)


def _push_branch(repo: Path, branch: str) -> None:
    subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", branch], capture_output=True, text=True, check=True)


def _parse_pr_number_from_url(url: str) -> int | None:
    match = re.search(r"/pull/(\d+)", url)
    return int(match.group(1)) if match else None
