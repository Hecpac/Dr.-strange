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
from claw_v2.kairos import TickDecision
from claw_v2.main import build_runtime
from claw_v2.scheduled_background_jobs import (
    KAIROS_TICK_JOB_KIND,
    KAIROS_TICK_RESUME_KEY,
    PERF_OPTIMIZER_JOB_KIND,
    PERF_OPTIMIZER_RESUME_KEY,
    WIKI_RESEARCH_JOB_KIND,
    WIKI_RESEARCH_RESUME_KEY,
    WIKI_SCRAPE_JOB_KIND,
    WIKI_SCRAPE_RESUME_KEY,
    ScheduledBackgroundJobRunner,
    enqueue_scheduled_background_job,
    kairos_tick_result_summary,
    safe_non_negative_int,
    wiki_research_result_summary,
    wiki_scrape_result_summary,
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
            kairos_first = enqueue_scheduled_background_job(
                job_name="kairos_tick",
                job_kind=KAIROS_TICK_JOB_KIND,
                resume_key=KAIROS_TICK_RESUME_KEY,
                job_service=jobs,
            )
            kairos_second = enqueue_scheduled_background_job(
                job_name="kairos_tick",
                job_kind=KAIROS_TICK_JOB_KIND,
                resume_key=KAIROS_TICK_RESUME_KEY,
                job_service=jobs,
            )
            scrape_first = enqueue_scheduled_background_job(
                job_name="wiki_scrape",
                job_kind=WIKI_SCRAPE_JOB_KIND,
                resume_key=WIKI_SCRAPE_RESUME_KEY,
                job_service=jobs,
            )
            scrape_second = enqueue_scheduled_background_job(
                job_name="wiki_scrape",
                job_kind=WIKI_SCRAPE_JOB_KIND,
                resume_key=WIKI_SCRAPE_RESUME_KEY,
                job_service=jobs,
            )

            self.assertEqual(wiki_first, wiki_second)
            self.assertEqual(perf_first, perf_second)
            self.assertEqual(kairos_first, kairos_second)
            self.assertEqual(scrape_first, scrape_second)
            self.assertNotEqual(wiki_first, perf_first)
            self.assertNotEqual(wiki_first, kairos_first)
            self.assertNotEqual(wiki_first, scrape_first)
            self.assertNotEqual(perf_first, kairos_first)
            self.assertNotEqual(perf_first, scrape_first)
            self.assertNotEqual(kairos_first, scrape_first)
            self.assertEqual(len(jobs.list(kinds=(WIKI_RESEARCH_JOB_KIND,), limit=10)), 1)
            self.assertEqual(len(jobs.list(kinds=(WIKI_SCRAPE_JOB_KIND,), limit=10)), 1)
            self.assertEqual(len(jobs.list(kinds=(PERF_OPTIMIZER_JOB_KIND,), limit=10)), 1)
            self.assertEqual(len(jobs.list(kinds=(KAIROS_TICK_JOB_KIND,), limit=10)), 1)
            active = jobs.list(
                statuses=("queued", "running", "retrying", "waiting_approval"),
                kinds=(
                    WIKI_RESEARCH_JOB_KIND,
                    WIKI_SCRAPE_JOB_KIND,
                    PERF_OPTIMIZER_JOB_KIND,
                    KAIROS_TICK_JOB_KIND,
                ),
                limit=10,
            )
            self.assertEqual(len(active), 4)

    def test_wiki_research_runner_completes_with_bounded_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = MagicMock()
            jobs = JobService(Path(tmpdir) / "claw.db")
            handler = MagicMock(
                return_value={
                    "topics_researched": 2,
                    "pages_written": 1,
                    "candidates_researched": 1,
                    "raw_sources_written": 1,
                    "candidates_blocked": 0,
                    "candidates_compiled": 1,
                    "compile_blocked": 0,
                    "compile_failed": 0,
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
                    "candidates_researched": 1,
                    "raw_sources_written": 1,
                    "candidates_blocked": 0,
                    "candidates_compiled": 1,
                    "compile_blocked": 0,
                    "compile_failed": 0,
                    "candidate_count": 1,
                    "candidate_previews": [
                        {
                            "slug": "",
                            "topic": "large raw candidate",
                            "category": "",
                            "status": "",
                            "source_query_count": 0,
                        }
                    ],
                },
            )
            self.assertNotIn("candidates", job.result)
            event_names = [call.args[0] for call in observe.emit.call_args_list]
            self.assertIn("wiki_research_job_started", event_names)
            self.assertIn("wiki_research_job_completed", event_names)

    def test_wiki_scrape_result_summary_keeps_bounded_source_diagnostics(self) -> None:
        result = wiki_scrape_result_summary(
            {
                "sources_scraped": 8,
                "pages_ingested": 0,
                "sources_skipped": 0,
                "source_results": [
                    {
                        "source": "Source A",
                        "url": "https://example.com/a",
                        "status": "scraped",
                        "items_extracted": 2,
                        "items_ingested": 0,
                        "items_skipped": 2,
                        "skip_reasons": {"duplicate": 1, "body_too_short": 1},
                        "raw_body": "must not persist",
                    }
                ],
                "item_results": [
                    {
                        "source": "Source A",
                        "title": "Duplicate Topic",
                        "slug": "duplicate-topic",
                        "status": "skipped",
                        "reason": "duplicate",
                        "body": "must not persist",
                    }
                ],
            }
        )

        self.assertEqual(result["sources_scraped"], 8)
        self.assertEqual(result["pages_ingested"], 0)
        self.assertEqual(result["sources_skipped"], 0)
        self.assertEqual(result["source_results"][0]["source"], "Source A")
        self.assertEqual(result["source_results"][0]["skip_reasons"]["duplicate"], 1)
        self.assertNotIn("raw_body", result["source_results"][0])
        self.assertEqual(result["item_results"][0]["reason"], "duplicate")
        self.assertNotIn("body", result["item_results"][0])

    def test_safe_non_negative_int_defaults_none_and_invalid_values(self) -> None:
        self.assertEqual(safe_non_negative_int(None, default=3), 3)
        self.assertEqual(safe_non_negative_int("not-an-int", default=3), 3)
        self.assertEqual(safe_non_negative_int(float("inf"), default=3), 3)
        self.assertEqual(safe_non_negative_int(-1, default=3), 0)
        self.assertEqual(safe_non_negative_int("2", default=3), 2)

    def test_kairos_tick_result_summary_is_bounded_and_redacted(self) -> None:
        decision = TickDecision(
            action="notify_user",
            reason='api_key = "secret with spaces"',
            detail="raw detail should not persist",
            duration_seconds=1.23456,
            error="token=sk-secret-value",
        )

        summary = kairos_tick_result_summary(decision)

        self.assertEqual(summary["action"], "notify_user")
        self.assertEqual(summary["duration_seconds"], 1.235)
        self.assertIn("REDACTED", summary["reason_preview"])
        self.assertNotIn("secret with spaces", summary["reason_preview"])
        self.assertIn("REDACTED", summary["error_preview"])
        self.assertNotIn("sk-secret-value", summary["error_preview"])
        self.assertNotIn("detail", summary)

    def test_kairos_tick_result_summary_defaults_none_action_to_unknown(self) -> None:
        decision = TickDecision(action=None)  # type: ignore[arg-type]

        summary = kairos_tick_result_summary(decision)

        self.assertEqual(summary["action"], "unknown")

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

    def test_stale_running_kairos_tick_job_is_reclaimed_and_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = MagicMock()
            jobs = JobService(Path(tmpdir) / "claw.db")
            handler = MagicMock(return_value=TickDecision(action="none"))
            enqueue_scheduled_background_job(
                job_name="kairos_tick",
                job_kind=KAIROS_TICK_JOB_KIND,
                resume_key=KAIROS_TICK_RESUME_KEY,
                job_service=jobs,
            )
            claimed_at = time.time() + 1
            stuck = jobs.claim_next(
                worker_id="dead-worker",
                kinds=(KAIROS_TICK_JOB_KIND,),
                now=claimed_at,
            )
            self.assertIsNotNone(stuck)
            runner = ScheduledBackgroundJobRunner(
                job_name="kairos_tick",
                job_kind=KAIROS_TICK_JOB_KIND,
                job_service=jobs,
                handler=handler,
                observe=observe,
                stale_running_seconds=1,
                result_summary=kairos_tick_result_summary,
            )

            processed = runner.run_available(now=claimed_at + 2)

            self.assertEqual(processed, 1)
            handler.assert_called_once()
            job = jobs.get(stuck.job_id)
            self.assertIsNotNone(job)
            self.assertEqual(job.status, "completed")
            self.assertEqual(job.attempts, 2)
            self.assertEqual(job.result["action"], "none")
            stale_events = [
                call.kwargs["payload"]
                for call in observe.emit.call_args_list
                if call.args[0] == "kairos_tick_job_stale_reclaimed"
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

    def test_kairos_tick_failure_retries_observably_and_daemon_tick_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = MagicMock()
            jobs = JobService(Path(tmpdir) / "claw.db")
            handler = MagicMock(side_effect=RuntimeError("boom token=sk-secret-value"))
            enqueue_scheduled_background_job(
                job_name="kairos_tick",
                job_kind=KAIROS_TICK_JOB_KIND,
                resume_key=KAIROS_TICK_RESUME_KEY,
                job_service=jobs,
            )
            runner = ScheduledBackgroundJobRunner(
                job_name="kairos_tick",
                job_kind=KAIROS_TICK_JOB_KIND,
                job_service=jobs,
                handler=handler,
                observe=observe,
                retry_delay_seconds=0,
                result_summary=kairos_tick_result_summary,
            )

            self.assertTrue(runner.run_once())

            job = jobs.list(kinds=(KAIROS_TICK_JOB_KIND,), limit=10)[0]
            self.assertEqual(job.status, "retrying")
            self.assertIn("REDACTED", job.error)
            self.assertNotIn("sk-secret-value", job.error)
            failed_events = [
                call.kwargs["payload"]
                for call in observe.emit.call_args_list
                if call.args[0] == "kairos_tick_job_failed"
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
            ("wiki_scrape", WIKI_SCRAPE_JOB_KIND, WIKI_SCRAPE_RESUME_KEY),
            ("perf_optimizer", PERF_OPTIMIZER_JOB_KIND, PERF_OPTIMIZER_RESUME_KEY),
            ("kairos_tick", KAIROS_TICK_JOB_KIND, KAIROS_TICK_RESUME_KEY),
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

            with patch.dict("os.environ", env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.wiki.auto_research = MagicMock(
                    return_value={"topics_researched": 1, "pages_written": 0, "candidates": []}
                )
                runtime.bot.wiki.auto_scrape_sources = MagicMock(
                    return_value={"sources_scraped": 1, "pages_ingested": 0, "sources_skipped": 0}
                )
                runtime.auto_research.run_loop = MagicMock()
                runtime.kairos.tick = MagicMock(return_value=TickDecision(action="none"))
                jobs = {job.name: job for job in runtime.scheduler.list_jobs()}

                jobs["kairos_tick"].handler()
                jobs["wiki_research"].handler()
                jobs["wiki_scrape"].handler()
                jobs["perf_optimizer"].handler()

                runtime.kairos.tick.assert_not_called()
                runtime.bot.wiki.auto_research.assert_not_called()
                runtime.bot.wiki.auto_scrape_sources.assert_not_called()
                runtime.auto_research.run_loop.assert_not_called()
                queued_kairos = runtime.job_service.list(kinds=(KAIROS_TICK_JOB_KIND,), limit=10)
                queued_wiki = runtime.job_service.list(kinds=(WIKI_RESEARCH_JOB_KIND,), limit=10)
                queued_scrape = runtime.job_service.list(kinds=(WIKI_SCRAPE_JOB_KIND,), limit=10)
                queued_perf = runtime.job_service.list(kinds=(PERF_OPTIMIZER_JOB_KIND,), limit=10)
                self.assertEqual(len(queued_kairos), 1)
                self.assertEqual(queued_kairos[0].status, "queued")
                self.assertEqual(len(queued_wiki), 1)
                self.assertEqual(queued_wiki[0].status, "queued")
                self.assertEqual(queued_wiki[0].payload["research_limit"], 1)
                self.assertEqual(queued_wiki[0].payload["compile_limit"], 1)
                self.assertEqual(len(queued_scrape), 1)
                self.assertEqual(queued_scrape[0].status, "queued")
                self.assertEqual(len(queued_perf), 1)
                self.assertEqual(queued_perf[0].status, "queued")
                disabled_skips = [
                    event
                    for event in runtime.observe.recent_events(limit=20)
                    if event["event_type"] == "scheduled_job_skipped"
                    and event["payload"].get("reason") == "autonomous_maintenance_disabled"
                ]
                self.assertEqual(disabled_skips, [])

    def test_runtime_scheduler_handlers_skip_when_maintenance_disabled(self) -> None:
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

            with patch.dict("os.environ", env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                jobs = {job.name: job for job in runtime.scheduler.list_jobs()}

                jobs["kairos_tick"].handler()

                queued_kairos = runtime.job_service.list(kinds=(KAIROS_TICK_JOB_KIND,), limit=10)
                self.assertEqual(queued_kairos, [])
                skips = [
                    event
                    for event in runtime.observe.recent_events(limit=20)
                    if event["event_type"] == "scheduled_job_skipped"
                ]
                self.assertEqual(len(skips), 1)
                self.assertEqual(skips[0]["payload"]["job"], "kairos_tick")
                self.assertEqual(
                    skips[0]["payload"]["reason"],
                    "autonomous_maintenance_disabled",
                )

    def test_autonomy_stale_recovery_runner_does_not_requeue_notebooklm_jobs(self) -> None:
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

            with patch.dict("os.environ", env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                research = runtime.job_service.enqueue(
                    kind="notebooklm.research",
                    max_attempts=3,
                )
                orchestration = runtime.job_service.enqueue(
                    kind="notebooklm.orchestrate",
                    max_attempts=3,
                )
                runtime.job_service.claim(
                    research.job_id,
                    worker_id="notebooklm",
                    now=time.time() - 7 * 60 * 60,
                )
                runtime.job_service.claim(
                    orchestration.job_id,
                    worker_id="notebooklm",
                    now=time.time() - 7 * 60 * 60,
                )
                runner = next(
                    runner
                    for runner in runtime.daemon._background_job_runners
                    if runner.name == "autonomy_stale_running_job_recovery"
                )

                recovered = runner.handler()

                self.assertEqual(recovered, 0)
                for created in (research, orchestration):
                    record = runtime.job_service.get(created.job_id)
                    self.assertEqual(record.status, "running")
                    self.assertEqual(record.worker_id, "notebooklm")

    def test_notebooklm_stale_reconcile_runner_fails_orphaned_jobs_terminally(self) -> None:
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

            with patch.dict("os.environ", env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                research = runtime.job_service.enqueue(kind="notebooklm.research", max_attempts=3)
                orchestration = runtime.job_service.enqueue(
                    kind="notebooklm.orchestrate", max_attempts=3
                )
                for created in (research, orchestration):
                    runtime.job_service.claim(
                        created.job_id,
                        worker_id="notebooklm",
                        now=time.time() - 7 * 60 * 60,
                    )
                runner = next(
                    runner
                    for runner in runtime.daemon._background_job_runners
                    if runner.name == "notebooklm_stale_running_job_reconcile"
                )

                reconciled = runner.handler()

                self.assertEqual(reconciled, 2)
                for created in (research, orchestration):
                    record = runtime.job_service.get(created.job_id)
                    self.assertEqual(record.status, "failed")
                    self.assertEqual(record.error, "stale_running_no_durable_consumer")
                    self.assertEqual(record.attempts, 1)

    def test_notebooklm_research_runner_registered_when_both_flags_on(self) -> None:
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
                "CLAW_F2_DURABILITY_ENABLED": "1",
                "CLAW_NOTEBOOKLM_RESEARCH_DURABLE": "1",
                "EVAL_ON_SELF_IMPROVE": "false",
            }

            with patch.dict("os.environ", env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)

            runner_names = {r.name for r in runtime.daemon._background_job_runners}
            self.assertIn("notebooklm_research", runner_names)

    def test_notebooklm_research_runner_not_registered_when_dedicated_flag_off(self) -> None:
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
                "CLAW_F2_DURABILITY_ENABLED": "1",
                "CLAW_NOTEBOOKLM_RESEARCH_DURABLE": "0",
                "EVAL_ON_SELF_IMPROVE": "false",
            }

            with patch.dict("os.environ", env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)

            runner_names = {r.name for r in runtime.daemon._background_job_runners}
            self.assertNotIn("notebooklm_research", runner_names)

    def test_notebooklm_research_runner_not_registered_when_f2_global_off(self) -> None:
        """§9 matrix row F2-global-OFF / dedicated-ON: both flags are required —
        the dedicated flag alone must not register the runner."""

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
                "CLAW_F2_DURABILITY_ENABLED": "0",
                "CLAW_NOTEBOOKLM_RESEARCH_DURABLE": "1",
                "EVAL_ON_SELF_IMPROVE": "false",
            }

            with patch.dict("os.environ", env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)

            runner_names = {r.name for r in runtime.daemon._background_job_runners}
            self.assertNotIn("notebooklm_research", runner_names)

    async def test_run_loop_processes_kairos_tick_job_outside_tick(self) -> None:
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

            with patch.dict("os.environ", env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.kairos.tick = MagicMock(
                    return_value=TickDecision(
                        action="none",
                        reason="nothing urgent",
                        duration_seconds=0.01,
                    )
                )
                enqueue_scheduled_background_job(
                    job_name="kairos_tick",
                    job_kind=KAIROS_TICK_JOB_KIND,
                    resume_key=KAIROS_TICK_RESUME_KEY,
                    job_service=runtime.job_service,
                    observe=runtime.observe,
                )
                shutdown = asyncio.Event()
                loop = asyncio.get_running_loop()

                async def stop_after_job() -> None:
                    deadline = loop.time() + 1.0
                    while loop.time() < deadline:
                        rows = runtime.job_service.list(kinds=(KAIROS_TICK_JOB_KIND,), limit=10)
                        if rows and rows[0].status == "completed":
                            shutdown.set()
                            return
                        await asyncio.sleep(0.01)
                    shutdown.set()

                await asyncio.gather(
                    runtime.daemon.run_loop(shutdown, interval=0.01),
                    stop_after_job(),
                )

                rows = runtime.job_service.list(kinds=(KAIROS_TICK_JOB_KIND,), limit=10)
                self.assertEqual(rows[0].status, "completed")
                self.assertEqual(rows[0].result["action"], "none")
                self.assertEqual(rows[0].result["reason_preview"], "nothing urgent")
                runtime.kairos.tick.assert_called_once_with()

    async def test_run_loop_processes_wiki_and_perf_jobs_outside_tick(self) -> None:
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

            with patch.dict("os.environ", env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.wiki.auto_research = MagicMock(
                    return_value={
                        "topics_researched": 1,
                        "pages_written": 0,
                        "candidates": [{"topic": "raw candidate body"}],
                    }
                )
                runtime.bot.wiki.auto_scrape_sources = MagicMock(
                    return_value={"sources_scraped": 1, "pages_ingested": 1, "sources_skipped": 0}
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
                    job_name="wiki_scrape",
                    job_kind=WIKI_SCRAPE_JOB_KIND,
                    resume_key=WIKI_SCRAPE_RESUME_KEY,
                    job_service=runtime.job_service,
                    observe=runtime.observe,
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
                        wiki_rows = runtime.job_service.list(
                            kinds=(WIKI_RESEARCH_JOB_KIND,), limit=10
                        )
                        scrape_rows = runtime.job_service.list(
                            kinds=(WIKI_SCRAPE_JOB_KIND,), limit=10
                        )
                        perf_rows = runtime.job_service.list(
                            kinds=(PERF_OPTIMIZER_JOB_KIND,), limit=10
                        )
                        if (
                            wiki_rows
                            and scrape_rows
                            and perf_rows
                            and wiki_rows[0].status == "completed"
                            and scrape_rows[0].status == "completed"
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
                scrape_rows = runtime.job_service.list(kinds=(WIKI_SCRAPE_JOB_KIND,), limit=10)
                perf_rows = runtime.job_service.list(kinds=(PERF_OPTIMIZER_JOB_KIND,), limit=10)
                self.assertEqual(wiki_rows[0].status, "completed")
                self.assertEqual(wiki_rows[0].result["candidate_count"], 1)
                self.assertNotIn("candidates", wiki_rows[0].result)
                self.assertEqual(scrape_rows[0].status, "completed")
                self.assertEqual(scrape_rows[0].result["sources_scraped"], 1)
                self.assertEqual(scrape_rows[0].result["pages_ingested"], 1)
                self.assertEqual(perf_rows[0].status, "completed")
                runtime.bot.wiki.auto_research.assert_called_once_with(
                    max_topics=3, research_limit=1, compile_limit=1
                )
                runtime.bot.wiki.auto_scrape_sources.assert_called_once_with()
                runtime.auto_research.run_loop.assert_called_once_with(
                    "perf-optimizer",
                    max_experiments=3,
                )


if __name__ == "__main__":
    unittest.main()
