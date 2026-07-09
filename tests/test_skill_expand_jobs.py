from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.adapters.base import LLMRequest, LLMResponse
from claw_v2.cron import CronScheduler, ScheduledJob
from claw_v2.daemon import ClawDaemon
from claw_v2.heartbeat import HeartbeatSnapshot
from claw_v2.jobs import JobService
from claw_v2.main import build_runtime
from claw_v2.skill_expand_jobs import (
    SKILL_EXPAND_JOB_KIND,
    SKILL_EXPAND_RESUME_KEY,
    SkillExpandJobRunner,
    enqueue_skill_expand_job,
)


class SkillExpandJobTests(unittest.TestCase):
    def test_enqueue_skill_expand_job_does_not_run_auto_expand_inline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = MagicMock()
            jobs = JobService(Path(tmpdir) / "claw.db")
            skill_registry = MagicMock()

            job_id = enqueue_skill_expand_job(job_service=jobs, observe=observe)

            self.assertIsNotNone(job_id)
            skill_registry.auto_expand.assert_not_called()
            queued = jobs.list(kinds=(SKILL_EXPAND_JOB_KIND,), limit=10)
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0].kind, SKILL_EXPAND_JOB_KIND)
            self.assertEqual(queued[0].status, "queued")
            self.assertEqual(queued[0].resume_key, SKILL_EXPAND_RESUME_KEY)
            observe.emit.assert_any_call(
                "scheduled_job_enqueued",
                payload={
                    "job": "skill_expand",
                    "job_id": queued[0].job_id,
                    "kind": SKILL_EXPAND_JOB_KIND,
                    "status": "queued",
                    "resume_key": SKILL_EXPAND_RESUME_KEY,
                },
            )

    def test_resume_key_prevents_duplicate_active_skill_expand_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs = JobService(Path(tmpdir) / "claw.db")

            first = enqueue_skill_expand_job(job_service=jobs)
            second = enqueue_skill_expand_job(job_service=jobs)

            self.assertEqual(first, second)
            queued = jobs.list(kinds=(SKILL_EXPAND_JOB_KIND,), limit=10)
            self.assertEqual(len(queued), 1)

    def test_runner_claims_and_completes_skill_expand_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = MagicMock()
            jobs = JobService(Path(tmpdir) / "claw.db")
            skill_registry = MagicMock()
            skill_registry.auto_expand.return_value = {"gaps_found": 1, "skills_generated": 0}
            enqueue_skill_expand_job(job_service=jobs, observe=observe, max_new=1)
            runner = SkillExpandJobRunner(
                job_service=jobs,
                skill_registry=skill_registry,
                observe=observe,
            )

            self.assertTrue(runner.run_once())

            skill_registry.auto_expand.assert_called_once_with(max_new=1)
            job = jobs.list(kinds=(SKILL_EXPAND_JOB_KIND,), limit=10)[0]
            self.assertEqual(job.status, "completed")
            self.assertEqual(job.result["gaps_found"], 1)
            event_names = [call.args[0] for call in observe.emit.call_args_list]
            self.assertIn("skill_expand_job_started", event_names)
            self.assertIn("skill_expand_job_completed", event_names)

    def test_run_once_closes_jobs_under_formal_leases(self) -> None:
        # Invariant (S1, analog of PR #240): with formal_leases_enabled the
        # runner must propagate the claimed JobRecord's lease credentials to
        # complete()/fail() so the durable row leaves 'running'.
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = MagicMock()
            jobs = JobService(Path(tmpdir) / "claw.db", formal_leases_enabled=True)
            skill_registry = MagicMock()
            skill_registry.auto_expand.return_value = {"gaps_found": 0, "skills_generated": 0}
            enqueue_skill_expand_job(job_service=jobs, observe=observe, max_new=1)
            runner = SkillExpandJobRunner(
                job_service=jobs,
                skill_registry=skill_registry,
                observe=observe,
            )

            self.assertTrue(runner.run_once())

            job = jobs.list(kinds=(SKILL_EXPAND_JOB_KIND,), limit=10)[0]
            self.assertEqual(job.status, "completed")
            self.assertIsNone(job.lease_owner)
            event_names = [call.args[0] for call in observe.emit.call_args_list]
            self.assertIn("skill_expand_job_completed", event_names)
            self.assertNotIn("skill_expand_job_lease_lost", event_names)

    def test_run_once_emits_lease_lost_when_close_does_not_land(self) -> None:
        # Invariant (D2, runner-side option): when complete()/fail() returns
        # None (lease guard blocked the close), the runner must emit
        # skill_expand_job_lease_lost instead of the lying
        # *_job_completed/_failed event.
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = MagicMock()
            jobs = JobService(Path(tmpdir) / "claw.db", formal_leases_enabled=True)
            skill_registry = MagicMock()
            skill_registry.auto_expand.return_value = {"gaps_found": 0, "skills_generated": 0}
            enqueue_skill_expand_job(job_service=jobs, observe=observe, max_new=1)
            runner = SkillExpandJobRunner(
                job_service=jobs,
                skill_registry=skill_registry,
                observe=observe,
            )
            # Force the guard to reject the close: the lease is stolen by
            # another worker mid-execution (expired + re-claimed), so the
            # original claimant's credentials no longer match.
            original_execute = runner._execute

            def steal_lease_then_execute(job):
                result = original_execute(job)
                jobs.reclaim_expired_leases(
                    kinds=(SKILL_EXPAND_JOB_KIND,),
                    now=time.time() + 100_000,
                )
                stolen = jobs.claim_next(
                    worker_id="thief",
                    kinds=(SKILL_EXPAND_JOB_KIND,),
                    now=time.time() + 100_001,
                )
                assert stolen is not None
                return result

            runner._execute = steal_lease_then_execute

            self.assertTrue(runner.run_once())

            event_names = [call.args[0] for call in observe.emit.call_args_list]
            self.assertIn("skill_expand_job_lease_lost", event_names)
            self.assertNotIn("skill_expand_job_completed", event_names)
            # The thief's claim stands untouched.
            job = jobs.list(kinds=(SKILL_EXPAND_JOB_KIND,), limit=10)[0]
            self.assertEqual(job.status, "running")
            self.assertEqual(job.lease_owner, "thief")

    def test_stale_running_skill_expand_job_is_reclaimed_and_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = MagicMock()
            jobs = JobService(Path(tmpdir) / "claw.db")
            skill_registry = MagicMock()
            skill_registry.auto_expand.return_value = {"gaps_found": 0, "skills_generated": 0}
            enqueue_skill_expand_job(job_service=jobs)
            claimed_at = time.time() + 1
            stuck = jobs.claim_next(
                worker_id="dead-worker",
                kinds=(SKILL_EXPAND_JOB_KIND,),
                now=claimed_at,
            )
            self.assertIsNotNone(stuck)
            runner = SkillExpandJobRunner(
                job_service=jobs,
                skill_registry=skill_registry,
                observe=observe,
                stale_running_seconds=1,
            )

            processed = runner.run_available(now=claimed_at + 2)

            self.assertEqual(processed, 1)
            job = jobs.get(stuck.job_id)
            self.assertIsNotNone(job)
            self.assertEqual(job.status, "completed")
            self.assertEqual(job.attempts, 2)
            stale_events = [
                call.kwargs["payload"]
                for call in observe.emit.call_args_list
                if call.args[0] == "skill_expand_job_stale_reclaimed"
            ]
            self.assertEqual(stale_events[0]["job_id"], stuck.job_id)

    def test_runner_failure_retries_observably_and_daemon_tick_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = MagicMock()
            jobs = JobService(Path(tmpdir) / "claw.db")
            skill_registry = MagicMock()
            skill_registry.auto_expand.side_effect = RuntimeError("boom api_key=sk-secret-value")
            enqueue_skill_expand_job(job_service=jobs)
            runner = SkillExpandJobRunner(
                job_service=jobs,
                skill_registry=skill_registry,
                observe=observe,
                retry_delay_seconds=0,
            )

            self.assertTrue(runner.run_once())

            job = jobs.list(kinds=(SKILL_EXPAND_JOB_KIND,), limit=10)[0]
            self.assertEqual(job.status, "retrying")
            self.assertIn("REDACTED", job.error)
            self.assertNotIn("sk-secret-value", job.error)
            failed_events = [
                call.kwargs["payload"]
                for call in observe.emit.call_args_list
                if call.args[0] == "skill_expand_job_failed"
            ]
            self.assertEqual(len(failed_events), 1)
            self.assertEqual(failed_events[0]["job_id"], job.job_id)
            self.assertEqual(failed_events[0]["error_type"], "RuntimeError")
            self.assertIn("REDACTED", failed_events[0]["error_preview"])
            self.assertNotIn("sk-secret-value", failed_events[0]["error_preview"])

            scheduler = CronScheduler()
            probe = MagicMock()
            scheduler.register(ScheduledJob(name="probe", interval_seconds=60, handler=probe))
            heartbeat = MagicMock()
            heartbeat.collect.return_value = HeartbeatSnapshot(
                timestamp="t",
                pending_approvals=0,
                pending_approval_ids=[],
                agents={},
                lane_metrics={},
            )
            daemon = ClawDaemon(scheduler=scheduler, heartbeat=heartbeat, observe=observe)

            result = daemon.tick(now=1_000_000)

            self.assertIn("probe", result.executed_jobs)
            probe.assert_called_once()

    def test_runner_respects_shutdown_before_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = MagicMock()
            jobs = JobService(Path(tmpdir) / "claw.db")
            skill_registry = MagicMock()
            enqueue_skill_expand_job(job_service=jobs)
            runner = SkillExpandJobRunner(
                job_service=jobs,
                skill_registry=skill_registry,
                observe=observe,
                should_stop=lambda: True,
            )

            self.assertEqual(runner.run_available(), 0)

            job = jobs.list(kinds=(SKILL_EXPAND_JOB_KIND,), limit=10)[0]
            self.assertEqual(job.status, "queued")
            skill_registry.auto_expand.assert_not_called()


class SkillExpandRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_runtime_scheduler_skill_expand_handler_enqueues_only(self) -> None:
        def fake_anthropic(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="<response>ok</response>", lane=req.lane, provider="anthropic"
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
                "WORKER_PROVIDER": "anthropic",
                "CLAW_AUTONOMOUS_MAINTENANCE": "true",
                "CLAW_AUTONOMOUS_MAINTENANCE_ENABLED": "true",
                "EVAL_ON_SELF_IMPROVE": "false",
            }
            from unittest.mock import patch

            with patch.dict("os.environ", env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.skill_registry.auto_expand = MagicMock(
                    return_value={"gaps_found": 1, "skills_generated": 0}
                )
                jobs = {job.name: job for job in runtime.scheduler.list_jobs()}

                jobs["skill_expand"].handler()

                runtime.skill_registry.auto_expand.assert_not_called()
                queued = runtime.job_service.list(kinds=(SKILL_EXPAND_JOB_KIND,), limit=10)
                self.assertEqual(len(queued), 1)
                self.assertEqual(queued[0].status, "queued")

    async def test_run_loop_processes_skill_expand_job_outside_tick(self) -> None:
        def fake_anthropic(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="<response>ok</response>", lane=req.lane, provider="anthropic"
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
                "WORKER_PROVIDER": "anthropic",
                "CLAW_AUTONOMOUS_MAINTENANCE": "false",
                "CLAW_AUTONOMOUS_MAINTENANCE_ENABLED": "false",
                "EVAL_ON_SELF_IMPROVE": "false",
            }
            from unittest.mock import patch

            with patch.dict("os.environ", env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.skill_registry.auto_expand = MagicMock(
                    return_value={"gaps_found": 1, "skills_generated": 0}
                )
                enqueue_skill_expand_job(job_service=runtime.job_service, observe=runtime.observe)
                shutdown = asyncio.Event()
                loop = asyncio.get_running_loop()

                async def stop_after_job() -> None:
                    deadline = loop.time() + 1.0
                    while loop.time() < deadline:
                        rows = runtime.job_service.list(kinds=(SKILL_EXPAND_JOB_KIND,), limit=10)
                        if rows and rows[0].status == "completed":
                            shutdown.set()
                            return
                        await asyncio.sleep(0.01)
                    shutdown.set()

                await asyncio.gather(
                    runtime.daemon.run_loop(shutdown, interval=0.01),
                    stop_after_job(),
                )

                rows = runtime.job_service.list(kinds=(SKILL_EXPAND_JOB_KIND,), limit=10)
                self.assertEqual(rows[0].status, "completed")
                runtime.skill_registry.auto_expand.assert_called_once_with(max_new=2)


if __name__ == "__main__":
    unittest.main()
