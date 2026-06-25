from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.adapters.base import LLMRequest, LLMResponse
from claw_v2.main import build_runtime
from claw_v2.scheduled_background_jobs import (
    APPROVAL_SWEEP_JOB_KIND,
    PIPELINE_POLL_MERGES_JOB_KIND,
)


def fake_anthropic(req: LLMRequest) -> LLMResponse:
    return LLMResponse(content="<response>ok</response>", lane=req.lane, provider="anthropic")


class ApprovalRuntimeWiringTests(unittest.TestCase):
    def test_startup_sweep_expires_old_pending_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            approvals_root = root / "approvals"
            approvals_root.mkdir(parents=True)
            approval_id = "approval-startup-old"
            (approvals_root / f"{approval_id}.json").write_text(
                json.dumps(
                    {
                        "approval_id": approval_id,
                        "action": "deploy",
                        "summary": "old approval",
                        "metadata": {},
                        "token_hash": "hash",
                        "status": "pending",
                        "created_at": time.time() - 60,
                    }
                ),
                encoding="utf-8",
            )
            env = _runtime_env(root, approvals_root=approvals_root)
            env["APPROVAL_TTL_SECONDS"] = "10"
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)

            self.assertEqual(runtime.approvals.status(approval_id), "expired")

    def test_periodic_approval_sweep_runs_off_tick(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = _runtime_env(root)
            env["APPROVAL_TTL_SECONDS"] = "1"
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                pending = runtime.approvals.create("deploy", "old approval")
                path = runtime.approvals._path_for(pending.approval_id)
                data = json.loads(path.read_text(encoding="utf-8"))
                data["created_at"] = time.time() - 10
                path.write_text(json.dumps(data), encoding="utf-8")

                scheduler_jobs = {job.name: job for job in runtime.scheduler.list_jobs()}
                runners = {runner.name: runner for runner in runtime.daemon._background_job_runners}
                self.assertIn("approval_sweep", scheduler_jobs)
                self.assertIn("approval_sweep", runners)

                with patch.object(
                    runtime.approvals,
                    "expire_due",
                    side_effect=AssertionError("scheduler must only enqueue approval sweep"),
                ):
                    scheduler_jobs["approval_sweep"].handler()

                rows = runtime.job_service.list(kinds=(APPROVAL_SWEEP_JOB_KIND,), limit=10)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].status, "queued")

                runners["approval_sweep"].handler()

                self.assertEqual(runtime.approvals.status(pending.approval_id), "expired")

    def test_maintenance_mode_blocks_approval_and_pipeline_merge_enqueues_with_f2_off(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = _runtime_env(root)
            env.update(
                {
                    "CLAW_MAINTENANCE_MODE": "1",
                    "CLAW_NO_JOB_CLAIM": "0",
                    "CLAW_AUTONOMOUS_MAINTENANCE": "true",
                    "CLAW_AUTONOMOUS_MAINTENANCE_ENABLED": "true",
                    "CLAW_F2_DURABILITY_ENABLED": "0",
                    "F2_DURABILITY_ENABLED": "0",
                }
            )
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)

                self.assertFalse(runtime.config.f2_durability_enabled)
                self.assertTrue(runtime.config.maintenance_mode_enabled)
                scheduler_jobs = {job.name: job for job in runtime.scheduler.list_jobs()}
                scheduler_jobs["approval_sweep"].handler()
                scheduler_jobs["pipeline_poll_merges"].handler()

                self.assertEqual(
                    runtime.job_service.list(kinds=(APPROVAL_SWEEP_JOB_KIND,), limit=10),
                    [],
                )
                self.assertEqual(
                    runtime.job_service.list(kinds=(PIPELINE_POLL_MERGES_JOB_KIND,), limit=10),
                    [],
                )
                skipped = [
                    event["payload"]
                    for event in runtime.observe.recent_events(limit=50)
                    if event["event_type"] == "scheduled_job_skipped"
                ]
                self.assertEqual(
                    {
                        payload["job"]: payload["reason"]
                        for payload in skipped
                        if payload["job"] in {"approval_sweep", "pipeline_poll_merges"}
                    },
                    {
                        "approval_sweep": "maintenance_mode_active",
                        "pipeline_poll_merges": "maintenance_mode_active",
                    },
                )
                assertions = [
                    event["payload"]
                    for event in runtime.observe.recent_events(limit=50)
                    if event["event_type"] == "maintenance_mode_gate_assertion"
                ]
                self.assertEqual(len(assertions), 1)
                self.assertEqual(
                    assertions[0]["message"],
                    "maintenance gates active: claim OFF, scheduler OFF, drain OFF",
                )
                self.assertEqual(assertions[0]["claim"], "off")
                self.assertEqual(assertions[0]["scheduler"], "off")
                self.assertEqual(assertions[0]["drain"], "off")
                self.assertFalse(assertions[0]["f2_durability_enabled"])

    def test_pipeline_poll_merges_preserves_autonomous_maintenance_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = _runtime_env(root)
            env.update(
                {
                    "CLAW_MAINTENANCE_MODE": "0",
                    "CLAW_NO_JOB_CLAIM": "0",
                    "CLAW_AUTONOMOUS_MAINTENANCE": "false",
                    "CLAW_AUTONOMOUS_MAINTENANCE_ENABLED": "false",
                }
            )
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                scheduler_jobs = {job.name: job for job in runtime.scheduler.list_jobs()}

                scheduler_jobs["pipeline_poll_merges"].handler()

                self.assertEqual(
                    runtime.job_service.list(kinds=(PIPELINE_POLL_MERGES_JOB_KIND,), limit=10),
                    [],
                )
                skips = [
                    event["payload"]
                    for event in runtime.observe.recent_events(limit=20)
                    if event["event_type"] == "scheduled_job_skipped"
                ]
                self.assertEqual(len(skips), 1)
                self.assertEqual(skips[0]["job"], "pipeline_poll_merges")
                self.assertEqual(skips[0]["reason"], "autonomous_maintenance_disabled")


def _runtime_env(root: Path, *, approvals_root: Path | None = None) -> dict[str, str]:
    return {
        "DB_PATH": str(root / "data" / "claw.db"),
        "WORKSPACE_ROOT": str(root / "workspace"),
        "AGENT_STATE_ROOT": str(root / "agents"),
        "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
        "APPROVALS_ROOT": str(approvals_root or root / "approvals"),
        "PIPELINE_STATE_ROOT": str(root / "pipeline"),
        "TELEGRAM_ALLOWED_USER_ID": "123",
        "COMPUTER_USE_ENABLED": "false",
        "WORKER_PROVIDER": "anthropic",
    }


if __name__ == "__main__":
    unittest.main()
