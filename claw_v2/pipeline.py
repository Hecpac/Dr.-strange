from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from claw_v2.approval import ApprovalManager
from claw_v2.github import GitHubPullRequestService
from claw_v2.linear import LinearIssue, LinearService
from claw_v2.llm import LLMRouter
from claw_v2.learning import LearningLoop
from claw_v2.memory import MemoryStore
from claw_v2.jobs import JobService, TERMINAL_JOB_STATES
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
    job_id: str | None = None


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
        memory: MemoryStore | None = None,
        learning: LearningLoop | None = None,
        jobs: JobService | None = None,
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
        self.memory = memory
        self.learning = learning
        self.jobs = jobs
        cleanup_stale_worktrees(default_repo_root)

    def process_issue(self, issue_id: str, *, repo_root: Path | None = None) -> PipelineRun:
        repo = repo_root or self.default_repo_root
        issue = self.linear.get_issue(issue_id)
        branch = _validate_branch_name(issue.branch_name or _slugify_branch(issue.id, issue.title))
        self.linear.update_status(issue_id, "In Progress")
        job = self._start_job(issue_id, payload={"phase": "process_issue", "branch": branch, "repo_root": str(repo)})
        run = PipelineRun(
            issue_id=issue_id,
            branch_name=branch,
            repo_root=str(repo),
            status="in_progress",
            job_id=job.job_id if job else None,
        )
        _create_branch(repo, branch)
        self._job_step(run, "branch_created", {"branch": branch})
        wt_path = _create_worktree(repo, branch)
        run.worktree_path = str(wt_path)
        self._job_step(run, "worktree_created", {"worktree_path": str(wt_path)})
        past_lessons = self.learning.retrieve_lessons(issue.description, task_type="pipeline")[0] if self.learning else _retrieve_lessons(self.memory, issue.description) if self.memory else ""
        try:
            for attempt in range(self.max_retries + 1):
                self._job_step(run, "attempt_started", {"attempt": attempt}, idempotency_key=f"{run.job_id}:attempt:{attempt}:started" if run.job_id else None)
                prompt = _build_code_prompt(issue, run, past_lessons=past_lessons)
                response = self.router.ask(
                    prompt, lane="worker", system_prompt="You are a coding agent. Implement the requested change.",
                    evidence_pack={"issue": issue.id, "description": issue.description},
                    cwd=str(wt_path),
                )
                self._job_step(run, "llm_completed", {"attempt": attempt, "model": response.model})
                run.diff = _collect_diff(wt_path)
                passed, output = _run_tests(wt_path)
                run.test_output = output
                self._job_step(run, "tests_completed", {"attempt": attempt, "passed": passed})
                if passed:
                    run.status = "awaiting_approval"
                    self._record(issue, run, "success")
                    break
                run.retries += 1
                if run.retries >= self.max_retries:
                    run.status = "failed"
                    self._record(issue, run, "failure")
                    self.linear.post_comment(issue_id, f"Pipeline failed after {self.max_retries} retries.\n\n```\n{output[:1000]}\n```")
                    self._job_fail(run, error=output[:500])
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
                self._job_waiting(run, {"approval_id": pending.approval_id})
        except Exception as exc:
            self._job_fail(run, error=str(exc)[:500])
            raise
        finally:
            self._save_run(run)
            if run.status == "failed" and wt_path:
                _remove_worktree(repo, wt_path)
        if self.observe:
            self.observe.emit("pipeline_checkpoint", payload={"issue": issue_id, "status": run.status})
        return run

    def complete_pipeline(self, issue_id: str) -> PipelineRun:
        run = self._load_run(issue_id)
        self._ensure_run_job(run, phase="complete_pipeline")
        if run.approval_id:
            status = self.approvals.status(run.approval_id)
            if status != "approved":
                run.status = "blocked"
                self._job_waiting(run, {"approval_status": status})
                self._save_run(run)
                return run
            self._job_step(run, "approval_checked", {"approval_id": run.approval_id, "status": status})
        repo = Path(run.repo_root)
        wt_path = Path(run.worktree_path) if run.worktree_path else None
        if wt_path and wt_path.exists():
            _commit_worktree(wt_path, f"feat: {issue_id} pipeline implementation")
            _push_branch(repo, run.branch_name)
            self._job_step(run, "branch_pushed", {"branch": run.branch_name})
        pr_result = self.pull_requests.create_pull_request(
            branch_name=run.branch_name,
            title=f"feat: {issue_id}",
            body=f"Automated PR from Claw pipeline.\n\nLinear: {issue_id}\n\nChanges:\n```\n{(run.diff or '')[:1000]}\n```",
            draft=False,
        )
        run.pr_url = pr_result.url
        run.status = "pr_created"
        self._job_step(run, "pull_request_created", {"pr_url": pr_result.url})
        self.linear.link_pr(issue_id, pr_result.url, pr_result.title)
        self.linear.update_status(issue_id, "In Review")
        if wt_path:
            _remove_worktree(repo, wt_path)
        self._save_run(run)
        self._job_complete(run, {"pr_url": run.pr_url, "pipeline_status": run.status})
        return run

    def _record(self, issue: LinearIssue, run: PipelineRun, outcome: str) -> None:
        """Record task outcome via LearningLoop (preferred) or legacy helper."""
        if self.learning:
            self.learning.record(
                task_type="pipeline",
                task_id=run.issue_id,
                description=f"{issue.title}: {issue.description[:200]}",
                approach=f"branch={run.branch_name}, diff_size={len(run.diff or '')} chars",
                outcome=outcome,
                error_snippet=(run.test_output or "")[:500] if outcome != "success" else None,
                retries=run.retries,
            )
        else:
            _record_outcome(self.memory, issue, run, outcome)

    def merge_and_close(self, issue_id: str) -> PipelineRun:
        """Merge the PR for a pipeline run and close the Linear issue."""
        run = self._load_run(issue_id)
        self._ensure_run_job(run, phase="merge_and_close")
        if run.status != "pr_created":
            return run
        if not run.pr_url:
            run.status = "failed"
            self._job_fail(run, error="missing pr_url")
            self._save_run(run)
            return run
        pr_number = _parse_pr_number_from_url(run.pr_url)
        if pr_number is None:
            run.status = "failed"
            self._job_fail(run, error="could not parse pr number")
            self._save_run(run)
            return run
        self.pull_requests.merge_pull_request(pr_number)
        self._job_step(run, "pull_request_merged", {"pr_number": pr_number})
        run.status = "merged"
        self._save_run(run)
        self.linear.update_status(issue_id, "Done")
        self.linear.post_comment(issue_id, f"PR merged and issue closed by Claw pipeline.\n\nPR: {run.pr_url}")
        run.status = "done"
        self._save_run(run)
        if self.learning:
            self.learning.record(
                task_type="pipeline",
                task_id=issue_id,
                description=f"Merged PR {run.pr_url}",
                approach=f"branch={run.branch_name}",
                outcome="success",
                lesson="Full cycle completed: issue → code → tests → PR → merge → done.",
            )
        if self.observe:
            self.observe.emit("pipeline_done", payload={"issue": issue_id, "pr_url": run.pr_url})
        self._job_complete(run, {"pr_url": run.pr_url, "pipeline_status": run.status})
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
        safe_id = Path(run.issue_id).name
        path = self.state_root / f"{safe_id}.json"
        path.write_text(json.dumps(asdict(run), indent=2), encoding="utf-8")

    def _pipeline_job_id(self, issue_id: str) -> str:
        return f"pipeline:{issue_id}"

    def _start_job(self, issue_id: str, *, payload: dict[str, Any]) -> object | None:
        if self.jobs is None:
            return None
        job_id = self._pipeline_job_id(issue_id)
        job = self.jobs.get(job_id)
        if job is not None and job.state in TERMINAL_JOB_STATES:
            job_id = f"{job_id}:{uuid.uuid4().hex}"
        job = self.jobs.enqueue(kind="pipeline", job_id=job_id, payload={"issue_id": issue_id, **payload})
        return self.jobs.start(job.job_id, lease_owner="pipeline")

    def _ensure_run_job(self, run: PipelineRun, *, phase: str) -> None:
        if self.jobs is None:
            return
        if run.job_id is None:
            job = self._start_job(run.issue_id, payload={"phase": phase, "branch": run.branch_name})
            run.job_id = job.job_id if job else None
        elif self.jobs.get(run.job_id) is None:
            self.jobs.enqueue(kind="pipeline", job_id=run.job_id, payload={"issue_id": run.issue_id, "phase": phase})
        if run.job_id:
            self.jobs.start(run.job_id, lease_owner="pipeline")

    def _job_step(self, run: PipelineRun, name: str, payload: dict[str, Any], *, idempotency_key: str | None = None) -> None:
        if self.jobs is not None and run.job_id:
            self.jobs.record_step(run.job_id, name, payload=payload, idempotency_key=idempotency_key)

    def _job_waiting(self, run: PipelineRun, payload: dict[str, Any]) -> None:
        if self.jobs is not None and run.job_id:
            self.jobs.waiting_approval(run.job_id, payload=payload)

    def _job_complete(self, run: PipelineRun, payload: dict[str, Any]) -> None:
        if self.jobs is not None and run.job_id:
            self.jobs.complete(run.job_id, payload=payload)

    def _job_fail(self, run: PipelineRun, *, error: str) -> None:
        if self.jobs is not None and run.job_id:
            self.jobs.fail(run.job_id, error=error)

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


_SAFE_BRANCH_RE = re.compile(r"^[a-zA-Z0-9._/-]+$")


def _validate_branch_name(branch: str) -> str:
    if not branch or not _SAFE_BRANCH_RE.match(branch):
        raise ValueError(f"Unsafe branch name: {branch!r}")
    if (
        branch.startswith(("-", "/", "."))
        or branch.endswith(("/", "."))
        or ".." in branch
        or "//" in branch
        or ".lock" in branch
        or "@{" in branch
        or "\\" in branch
    ):
        raise ValueError(f"Unsafe branch name: {branch!r}")
    return branch


def _slugify_branch(issue_id: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
    return f"feat/{issue_id.lower()}-{slug}"


def _build_code_prompt(issue: LinearIssue, run: PipelineRun, *, past_lessons: str = "") -> str:
    parts = [f"Implement the following issue: {issue.id} — {issue.title}", "", issue.description]
    if past_lessons:
        parts.extend(["", "# Lessons from similar past tasks", past_lessons])
    if run.retries > 0 and run.test_output:
        parts.extend(["", f"Previous attempt failed. Test output:", f"```\n{run.test_output[:2000]}\n```", "Fix the failing tests."])
    return "\n".join(parts)


def _create_branch(repo: Path, branch: str) -> None:
    subprocess.run(["git", "-C", str(repo), "branch", "--no-track", "--", branch, "HEAD"], capture_output=True, text=True, check=False)


def cleanup_stale_worktrees(repo: Path) -> int:
    wt_root = repo.parent / ".claw-worktrees"
    if not wt_root.exists():
        return 0
    cleaned = 0
    for child in wt_root.iterdir():
        if child.is_dir():
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(child)], capture_output=True, text=True, check=False)
            if child.exists():
                shutil.rmtree(child, ignore_errors=True)
            cleaned += 1
    subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True, text=True, check=False)
    return cleaned


def _create_worktree(repo: Path, branch: str) -> Path:
    wt_path = repo.parent / ".claw-worktrees" / branch.replace("/", "-")
    if wt_path.exists():
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt_path)], capture_output=True, text=True, check=False)
        if wt_path.exists():
            shutil.rmtree(wt_path)
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--", str(wt_path), branch], capture_output=True, text=True, check=True)
    return wt_path


def _remove_worktree(repo: Path, wt_path: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt_path)], capture_output=True, text=True, check=False)
    if wt_path.exists():
        shutil.rmtree(wt_path, ignore_errors=True)


def _collect_diff(wt_path: Path) -> str:
    result = subprocess.run(["git", "-C", str(wt_path), "diff", "--", "."], capture_output=True, text=True, check=False)
    status = subprocess.run(["git", "-C", str(wt_path), "status", "--porcelain"], capture_output=True, text=True, check=False)
    return (result.stdout or "") + "\n" + (status.stdout or "")


def _run_tests(wt_path: Path, *, timeout: int = 300) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "-x", "-q"],
            cwd=str(wt_path), capture_output=True, text=True, check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"Tests timed out after {timeout}s"
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output


def _commit_worktree(wt_path: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(wt_path), "add", "-A"], capture_output=True, text=True, check=True)
    subprocess.run(["git", "-C", str(wt_path), "commit", "-m", message, "--allow-empty"], capture_output=True, text=True, check=False)


def _push_branch(repo: Path, branch: str) -> None:
    subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", "--", branch], capture_output=True, text=True, check=True)


def _parse_pr_number_from_url(url: str) -> int | None:
    match = re.search(r"/pull/(\d+)", url)
    return int(match.group(1)) if match else None


def _retrieve_lessons(memory: MemoryStore | None, description: str) -> str:
    if not memory:
        return ""
    keywords = " ".join(description.split()[:20])
    outcomes = memory.search_past_outcomes(keywords, task_type="pipeline", limit=3)
    if not outcomes:
        failures = memory.recent_failures(task_type="pipeline", limit=3)
        outcomes = failures
    if not outcomes:
        return ""
    lines: list[str] = []
    for o in outcomes:
        status = "SUCCESS" if o["outcome"] == "success" else "FAILED"
        lines.append(f"- [{status}] {o['description'][:80]}")
        lines.append(f"  Lesson: {o['lesson']}")
        if o.get("error_snippet"):
            lines.append(f"  Error: {o['error_snippet'][:200]}")
    return "\n".join(lines)


def _record_outcome(
    memory: MemoryStore | None, issue: LinearIssue, run: PipelineRun, outcome: str,
) -> None:
    if not memory:
        return
    lesson = _derive_lesson(run, outcome)
    memory.store_task_outcome(
        task_type="pipeline",
        task_id=run.issue_id,
        description=f"{issue.title}: {issue.description[:200]}",
        approach=f"branch={run.branch_name}, diff_size={len(run.diff or '')} chars",
        outcome=outcome,
        lesson=lesson,
        error_snippet=(run.test_output or "")[:500] if outcome == "failure" else None,
        retries=run.retries,
    )


def _derive_lesson(run: PipelineRun, outcome: str) -> str:
    if outcome == "success":
        if run.retries == 0:
            return "Resolved on first attempt."
        return f"Resolved after {run.retries} retries. Test failures guided the fix."
    output = (run.test_output or "").lower()
    if "import" in output and "error" in output:
        return "Import errors — check module paths and dependencies."
    if "assert" in output:
        return "Assertion failures — verify expected values match implementation."
    if "timeout" in output:
        return "Test timeouts — check for infinite loops or slow operations."
    if "permission" in output:
        return "Permission errors — check file/directory access rights."
    return f"Failed after {run.retries} retries. Review test output for root cause."
