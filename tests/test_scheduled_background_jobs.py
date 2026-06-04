from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from claw_v2.adapters.base import LLMRequest, LLMResponse
from claw_v2.cron import CronScheduler, ScheduledJob
from claw_v2.daemon import ClawDaemon
from claw_v2.heartbeat import HeartbeatSnapshot
from claw_v2.jobs import JobService
from claw_v2.main import build_runtime
from claw_v2.scheduled_background_jobs import (
    PERF_OPTIMIZER_JOB_KIND,
    PERF_OPTIMIZER_RESUME_KEY,
    WIKI_RESEARCH_JOB_KIND,
    WIKI_RESEARCH_RESUME_KEY,
    ScheduledBackgroundJobRunner,
    enqueue_scheduled_background_job,
    safe_non_negative_int,
    wiki_research_result_summary,
)


class ScheduledBackgroundJobTests(unittest.TestCase):
    def test_wiki_research_enqueue_does_not_run_inline_and_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = MagicMock()
            jobs = JobService(Path(tmpdir) / "claw.db")
            handler = MagicMock()

            first = enqueue_scheduled_background_job(
                job_name="wiki_research",
                job_kind=WIKI_RESEARCH_JOB_KIND,
                resume_key=WIKI_RESEARCH_RESUME_KEY,
                job_service=jobs,
                observe=observe,
                payload={"max_topics": 3},
            )
            second = enqueue_scheduled_background_job(
                job_name="wiki_research",
                job_kind=WIKI_RESEARCH_JOB_KIND,
                resume_key=WIKI_RESEARCH_RESUME_KEY,
                job_service=jobs,
                observe=observe,
                payload={"max_topics": 3},
            )

            handler.assert_not_called()
            self.assertEqual(first, second)
            queued = jobs.list(kinds=(WIKI_RESEARCH_JOB_KIND,), limit=10)
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0].status, "queued")
            self.assertEqual(queued[0].resume_key, WIKI_RESEARCH_RESUME_KEY)
            observe.emit.assert_any_call(
                "scheduled_job_enqueued",
                payload={
                    "job": "wiki_research",
                    "job_id": queued[0].job_id,
                    "kind": WIKI_RESEARCH_JOB_KIND,
                    "status": "queued",
                    "resume_key": WIKI_RESEARCH_RESUME_KEY,
                },
            )

    def test_resume_keys_dedupe_per_job_without_cross_kind_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs = JobService(Path(tmpdir) / "claw.db")

            wiki_first = enqueue_scheduled_background_job(
                job_name="wiki_research",
                job_kind=WIKI_RESEARCH_JOB_KIND,
                resume_key=WIKI_RESEARCH_RESUME_KEY,
                job_service=jobs,
            )
            wiki_second = enqueue_scheduled_background_job(
                job_name="wiki_research",
                job_kind=WIKI_RESEARCH_JOB_KIND,
                resume_key=WIKI_RESEARCH_RESUME_KEY,
                job_service=jobs,
            )
            perf_first = enqueue_scheduled_background_job(
                job_name="perf_optimizer",
                job_kind=PERF_OPTIMIZER_JOB_KIND,
                resume_key=PERF_OPTIMIZER_RESUME_KEY,
                job_service=jobs,
            )
            perf_second = enqueue_scheduled_background_job(
                job_name="perf_optimizer",
                job_kind=PERF_OPTIMIZER_JOB_KIND,
                resume_key=PERF_OPTIMIZER_RESUME_KEY,
                job_service=jobs,
            )

            self.assertEqual(wiki_first, wiki_second)
            self.assertEqual(perf_first, perf_second)
            self.assertNotEqual(wiki_first, perf_first)
            self.assertEqual(len(jobs.list(kinds=(WIKI_RESEARCH_JOB_KIND,), limit=10)), 1)
            self.assertEqual(len(jobs.list(kinds=(PERF_OPTIMIZER_JOB_KIND,), limit=10)), 1)
            active = jobs.list(
                statuses=("queued", "running", "retrying", "waiting_approval"),
                kinds=(WIKI_RESEARCH_JOB_KIND, PERF_OPTIMIZER_JOB_KIND),
                limit=10,
            )
            self.assertEqual(len(active), 2)

    def test_wiki_research_runner_completes_with_bounded_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = MagicMock()
            jobs = JobService(Path(tmpdir) / "claw.db")
            handler = MagicMock(
                return_value={
                    "topics_researched": 2,
                    "pages_written": 1,
                    "candidates": [{"topic": "large raw candidate"}],
                }
            )
            enqueue_scheduled_background_job(
                job_name="wiki_research",
                job_kind=WIKI_RESEARCH_JOB_KIND,
                resume_key=WIKI_RESEARCH_RESUME_KEY,
                job_service=jobs,
                payload={"max_topics": 2},
            )
            runner = ScheduledBackgroundJobRunner(
                job_name="wiki_research",
                job_kind=WIKI_RESEARCH_JOB_KIND,
                job_service=jobs,
                handler=handler,
                observe=observe,
                result_summary=wiki_research_result_summary,
            )

            self.assertTrue(runner.run_once())

            job = jobs.list(kinds=(WIKI_RESEARCH_JOB_KIND,), limit=10)[0]
            handler.assert_called_once()
            called_payload = handler.call_args.args[0]
            self.assertEqual(called_payload["max_topics"], 2)
            self.assertIn("requested_at", called_payload)
            self.assertEqual(job.status, "completed")
            self.assertEqual(
                job.result,
                {
                    "topics_researched": 2,
                    "pages_written": 1,
                    "candidate_count": 1,
                },
            )
            self.assertNotIn("candidates", job.result)
            event_names = [call.args[0] for call in observe.emit.call_args_list]
            self.assertIn("wiki_research_job_started", event_names)
            self.assertIn("wiki_research_job_completed", event_names)

    def test_safe_non_negative_int_defaults_none_and_invalid_values(self) -> None:
        self.assertEqual(safe_non_negative_int(None, default=3), 3)
        self.assertEqual(safe_non_negative_int("not-an-int", default=3), 3)
        self.assertEqual(safe_non_negative_int(float("inf"), default=3), 3)
        self.assertEqual(safe_non_negative_int(-1, default=3), 0)
        self.assertEqual(safe_non_negative_int("2", default=3), 2)

    def test_stale_running_perf_optimizer_job_is_reclaimed_and_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = MagicMock()
            jobs = JobService(Path(tmpdir) / "claw.db")
            handler = MagicMock(return_value=None)
            enqueue_scheduled_background_job(
                job_name="perf_optimizer",
                job_kind=PERF_OPTIMIZER_JOB_KIND,
                resume_key=PERF_OPTIMIZER_RESUME_KEY,
                job_service=jobs,
            )
            claimed_at = time.time() + 1
            stuck = jobs.claim_next(
                worker_id="dead-worker",
                kinds=(PERF_OPTIMIZER_JOB_KIND,),
                now=claimed_at,
            )
            self.assertIsNotNone(stuck)
            runner = ScheduledBackgroundJobRunner(
                job_name="perf_optimizer",
                job_kind=PERF_OPTIMIZER_JOB_KIND,
                job_service=jobs,
                handler=handler,
                observe=observe,
                stale_running_seconds=1,
            )

            processed = runner.run_available(now=claimed_at + 2)

            self.assertEqual(processed, 1)
            handler.assert_called_once()
            job = jobs.get(stuck.job_id)
            self.assertIsNotNone(job)
            self.assertEqual(job.status, "completed")
            self.assertEqual(job.attempts, 2)
            stale_events = [
                call.kwargs["payload"]
                for call in observe.emit.call_args_list
                if call.args[0] == "perf_optimizer_job_stale_reclaimed"
            ]
            self.assertEqual(stale_events[0]["job_id"], stuck.job_id)

    def test_runner_failure_retries_observably_and_daemon_tick_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = MagicMock()
            jobs = JobService(Path(tmpdir) / "claw.db")
            handler = MagicMock(side_effect=RuntimeError('boom api_key = "secret with spaces"'))
            enqueue_scheduled_background_job(
                job_name="wiki_research",
                job_kind=WIKI_RESEARCH_JOB_KIND,
                resume_key=WIKI_RESEARCH_RESUME_KEY,
                job_service=jobs,
            )
            runner = ScheduledBackgroundJobRunner(
                job_name="wiki_research",
                job_kind=WIKI_RESEARCH_JOB_KIND,
                job_service=jobs,
                handler=handler,
                observe=observe,
                retry_delay_seconds=0,
            )

            self.assertTrue(runner.run_once())

            job = jobs.list(kinds=(WIKI_RESEARCH_JOB_KIND,), limit=10)[0]
            self.assertEqual(job.status, "retrying")
            self.assertIn("REDACTED", job.error)
            self.assertNotIn("secret with spaces", job.error)
            failed_events = [
                call.kwargs["payload"]
                for call in observe.emit.call_args_list
                if call.args[0] == "wiki_research_job_failed"
            ]
            self.assertEqual(len(failed_events), 1)
            self.assertEqual(failed_events[0]["job_id"], job.job_id)
            self.assertEqual(failed_events[0]["error_type"], "RuntimeError")
            self.assertIn("REDACTED", failed_events[0]["error_preview"])
            self.assertNotIn("secret with spaces", failed_events[0]["error_preview"])

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

    def test_perf_optimizer_failure_retries_observably_and_daemon_tick_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = MagicMock()
            jobs = JobService(Path(tmpdir) / "claw.db")
            handler = MagicMock(side_effect=RuntimeError("boom api_key=sk-secret-value"))
            enqueue_scheduled_background_job(
                job_name="perf_optimizer",
                job_kind=PERF_OPTIMIZER_JOB_KIND,
                resume_key=PERF_OPTIMIZER_RESUME_KEY,
                job_service=jobs,
            )
            runner = ScheduledBackgroundJobRunner(
                job_name="perf_optimizer",
                job_kind=PERF_OPTIMIZER_JOB_KIND,
                job_service=jobs,
                handler=handler,
                observe=observe,
                retry_delay_seconds=0,
            )

            self.assertTrue(runner.run_once())

            job = jobs.list(kinds=(PERF_OPTIMIZER_JOB_KIND,), limit=10)[0]
            self.assertEqual(job.status, "retrying")
            self.assertIn("REDACTED", job.error)
            self.assertNotIn("sk-secret-value", job.error)
            failed_events = [
                call.kwargs["payload"]
                for call in observe.emit.call_args_list
                if call.args[0] == "perf_optimizer_job_failed"
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

    def test_runner_respects_shutdown_before_claim_for_each_kind(self) -> None:
        cases = (
            ("wiki_research", WIKI_RESEARCH_JOB_KIND, WIKI_RESEARCH_RESUME_KEY),
            ("perf_optimizer", PERF_OPTIMIZER_JOB_KIND, PERF_OPTIMIZER_RESUME_KEY),
        )
        for job_name, job_kind, resume_key in cases:
            with self.subTest(job_name=job_name), tempfile.TemporaryDirectory() as tmpdir:
                observe = MagicMock()
                jobs = JobService(Path(tmpdir) / "claw.db")
                handler = MagicMock()
                enqueue_scheduled_background_job(
                    job_name=job_name,
                    job_kind=job_kind,
                    resume_key=resume_key,
                    job_service=jobs,
                )
                runner = ScheduledBackgroundJobRunner(
                    job_name=job_name,
                    job_kind=job_kind,
                    job_service=jobs,
                    handler=handler,
                    observe=observe,
                    should_stop=lambda: True,
                )

                self.assertEqual(runner.run_available(), 0)

                job = jobs.list(kinds=(job_kind,), limit=10)[0]
                self.assertEqual(job.status, "queued")
                handler.assert_not_called()


class ScheduledBackgroundRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_runtime_scheduler_handlers_enqueue_only(self) -> None:
        def fake_anthropic(req: LLMRequest) -> LLMResponse:
            return LLMResponse(content="<response>ok</response>", lane=req.lane, provider="anthropic")

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

            with patch.dict("os.environ", env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.wiki.auto_research = MagicMock(
                    return_value={"topics_researched": 1, "pages_written": 0, "candidates": []}
                )
                runtime.auto_research.run_loop = MagicMock()
                jobs = {job.name: job for job in runtime.scheduler.list_jobs()}

                jobs["wiki_research"].handler()
                jobs["perf_optimizer"].handler()

                runtime.bot.wiki.auto_research.assert_not_called()
                runtime.auto_research.run_loop.assert_not_called()
                queued_wiki = runtime.job_service.list(kinds=(WIKI_RESEARCH_JOB_KIND,), limit=10)
                queued_perf = runtime.job_service.list(kinds=(PERF_OPTIMIZER_JOB_KIND,), limit=10)
                self.assertEqual(len(queued_wiki), 1)
                self.assertEqual(queued_wiki[0].status, "queued")
                self.assertEqual(len(queued_perf), 1)
                self.assertEqual(queued_perf[0].status, "queued")

    async def test_run_loop_processes_wiki_and_perf_jobs_outside_tick(self) -> None:
        def fake_anthropic(req: LLMRequest) -> LLMResponse:
            return LLMResponse(content="<response>ok</response>", lane=req.lane, provider="anthropic")

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

            with patch.dict("os.environ", env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.wiki.auto_research = MagicMock(
                    return_value={
                        "topics_researched": 1,
                        "pages_written": 0,
                        "candidates": [{"topic": "raw candidate body"}],
                    }
                )
                runtime.agent_store.state_path = MagicMock(
                    return_value=SimpleNamespace(exists=lambda: True)
                )
                runtime.auto_research.inspect = MagicMock(return_value={"paused": False})
                runtime.auto_research.run_loop = MagicMock(
                    return_value=SimpleNamespace(
                        experiments_run=1,
                        paused=False,
                        reason="ok",
                        last_metric=0.9,
                    )
                )
                enqueue_scheduled_background_job(
                    job_name="wiki_research",
                    job_kind=WIKI_RESEARCH_JOB_KIND,
                    resume_key=WIKI_RESEARCH_RESUME_KEY,
                    job_service=runtime.job_service,
                    observe=runtime.observe,
                    payload={"max_topics": None},
                )
                enqueue_scheduled_background_job(
                    job_name="perf_optimizer",
                    job_kind=PERF_OPTIMIZER_JOB_KIND,
                    resume_key=PERF_OPTIMIZER_RESUME_KEY,
                    job_service=runtime.job_service,
                    observe=runtime.observe,
                )
                shutdown = asyncio.Event()
                loop = asyncio.get_running_loop()

                async def stop_after_jobs() -> None:
                    deadline = loop.time() + 1.0
                    while loop.time() < deadline:
                        wiki_rows = runtime.job_service.list(kinds=(WIKI_RESEARCH_JOB_KIND,), limit=10)
                        perf_rows = runtime.job_service.list(kinds=(PERF_OPTIMIZER_JOB_KIND,), limit=10)
                        if (
                            wiki_rows
                            and perf_rows
                            and wiki_rows[0].status == "completed"
                            and perf_rows[0].status == "completed"
                        ):
                            shutdown.set()
                            return
                        await asyncio.sleep(0.01)
                    shutdown.set()

                await asyncio.gather(
                    runtime.daemon.run_loop(shutdown, interval=0.01),
                    stop_after_jobs(),
                )

                wiki_rows = runtime.job_service.list(kinds=(WIKI_RESEARCH_JOB_KIND,), limit=10)
                perf_rows = runtime.job_service.list(kinds=(PERF_OPTIMIZER_JOB_KIND,), limit=10)
                self.assertEqual(wiki_rows[0].status, "completed")
                self.assertEqual(wiki_rows[0].result["candidate_count"], 1)
                self.assertNotIn("candidates", wiki_rows[0].result)
                self.assertEqual(perf_rows[0].status, "completed")
                runtime.bot.wiki.auto_research.assert_called_once_with(max_topics=3)
                runtime.auto_research.run_loop.assert_called_once_with(
                    "perf-optimizer",
                    max_experiments=3,
                )


if __name__ == "__main__":
    unittest.main()
