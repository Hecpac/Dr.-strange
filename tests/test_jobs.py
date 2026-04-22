from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from claw_v2.approval import ApprovalManager
from claw_v2.job_commands import JobCommandPlugin
from claw_v2.job_records import JOB_SCHEMA_VERSION
from claw_v2.jobs import JobService
from claw_v2.linear import LinearIssue
from claw_v2.notebooklm import NotebookLMService
from claw_v2.observe import ObserveStream
from claw_v2.pipeline import PipelineService
from claw_v2.types import LLMResponse


def test_job_service_persists_lifecycle_steps_and_lineage(tmp_path: Path) -> None:
    observe = ObserveStream(tmp_path / "claw.db")
    jobs = JobService(tmp_path / "claw.db", observe=observe)

    job = jobs.enqueue(kind="pipeline", job_id="pipeline:HEC-1", payload={"issue_id": "HEC-1"})
    jobs.start(job.job_id, lease_owner="worker-1")
    artifact_id = jobs.checkpoint(job.job_id, "diff_collected", {"files": 2})
    jobs.waiting_approval(job.job_id, {"approval_id": "approval-1"})

    restarted = JobService(tmp_path / "claw.db", observe=observe)
    loaded = restarted.get(job.job_id)

    assert loaded is not None
    assert loaded.state == "waiting_approval"
    assert loaded.payload["approval_id"] == "approval-1"
    assert restarted.steps(job.job_id)[0].result_artifact_id == artifact_id
    assert observe.artifact_lineage(artifact_id)[0].job_id == job.job_id


def test_job_steps_are_idempotent_by_key(tmp_path: Path) -> None:
    jobs = JobService(tmp_path / "claw.db")
    job = jobs.enqueue(kind="notebooklm.research", payload={"notebook_id": "nb-1"})

    first = jobs.record_step(job.job_id, "import_sources", payload={"count": 3}, idempotency_key="nb-1:import")
    second = jobs.record_step(job.job_id, "import_sources", payload={"count": 99}, idempotency_key="nb-1:import")

    assert second.step_id == first.step_id
    assert len(jobs.steps(job.job_id)) == 1


def test_job_schema_sets_user_version_without_downgrade(tmp_path: Path) -> None:
    db_path = tmp_path / "future.db"
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version={JOB_SCHEMA_VERSION + 10}")
    conn.close()

    jobs = JobService(db_path)

    assert jobs._conn.execute("PRAGMA user_version").fetchone()[0] == JOB_SCHEMA_VERSION + 10


def test_job_commands_list_status_and_cancel(tmp_path: Path) -> None:
    jobs = JobService(tmp_path / "claw.db")
    job = jobs.enqueue(kind="pipeline", job_id="pipeline:HEC-1", payload={"issue_id": "HEC-1"})
    jobs.start(job.job_id)
    plugin = JobCommandPlugin(SimpleNamespace(job_service=jobs))

    listing = plugin._handle_jobs_command(_ctx("/jobs"))
    status = plugin._handle_jobs_command(_ctx("/job_status pipeline:HEC-1"))
    cancelled = plugin._handle_jobs_command(_ctx("/job_cancel pipeline:HEC-1"))

    assert "pipeline:HEC-1" in listing
    assert '"state": "running"' in status
    assert '"state": "cancelled"' in cancelled


def test_pipeline_records_durable_job_until_approval(tmp_path: Path) -> None:
    linear = MagicMock()
    linear.get_issue.return_value = LinearIssue(
        id="HEC-1",
        title="Fix login",
        description="Users cannot log in",
        state="Todo",
        labels=["claw-auto"],
        branch_name="feat/hec-1-login",
        url="https://linear.app/issue/HEC-1",
    )
    router = MagicMock()
    router.ask.return_value = LLMResponse(content="done", lane="worker", provider="anthropic", model="sonnet")
    approvals = ApprovalManager(tmp_path / "approvals", "secret")
    jobs = JobService(tmp_path / "claw.db")
    svc = PipelineService(
        linear=linear,
        router=router,
        approvals=approvals,
        pull_requests=MagicMock(),
        observe=None,
        default_repo_root=tmp_path,
        state_root=tmp_path / "pipeline",
        jobs=jobs,
    )

    with patch("claw_v2.pipeline._create_branch"), patch("claw_v2.pipeline._create_worktree", return_value=tmp_path / "wt"), patch("claw_v2.pipeline._collect_diff", return_value="diff"), patch("claw_v2.pipeline._run_tests", return_value=(True, "5 passed")):
        run = svc.process_issue("HEC-1")

    job = jobs.get(run.job_id or "")
    assert job is not None
    assert job.state == "waiting_approval"
    assert job.payload["approval_id"] == run.approval_id
    assert {"branch_created", "worktree_created", "tests_completed"} <= {step.name for step in jobs.steps(job.job_id)}


def test_notebooklm_background_research_updates_job_state(tmp_path: Path) -> None:
    jobs = JobService(tmp_path / "claw.db")
    svc = NotebookLMService(jobs=jobs)
    svc._sdk_available = False
    svc._cdp_research_fn = lambda notebook_id, query: 2

    message = svc.start_research("nb-full-id", "AI trends")

    deadline = time.time() + 2.0
    while time.time() < deadline and svc._running:
        time.sleep(0.01)

    job_id = message.split("Job: ", 1)[1]
    job = jobs.get(job_id)
    assert job is not None
    assert job.state == "completed"
    assert job.payload["sources_count"] == 2
    assert jobs.steps(job_id)[0].name == "research_completed"


def _ctx(text: str):
    return SimpleNamespace(user_id="123", session_id="s1", text=text, stripped=text)
