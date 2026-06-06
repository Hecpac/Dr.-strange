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
from claw_v2.scheduled_background_jobs import APPROVAL_SWEEP_JOB_KIND


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
