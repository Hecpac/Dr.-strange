from __future__ import annotations

import asyncio
import threading
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from claw_v2.adapters.base import LLMRequest, LLMResponse
from claw_v2.cron import CronScheduler, ScheduledJob
from claw_v2.daemon import ClawDaemon
from claw_v2.heartbeat import HeartbeatSnapshot
from claw_v2.jobs import JobService
from claw_v2.kairos import TickDecision
from claw_v2.main import build_runtime
from claw_v2.morning_brief import MorningBriefService, MorningBriefSettings
from claw_v2.scheduled_background_jobs import (
    EVENING_BRIEF_JOB_KIND,
    EVENING_BRIEF_RESUME_ID,
    KAIROS_TICK_JOB_KIND,
    KAIROS_TICK_RESUME_KEY,
    MORNING_BRIEF_JOB_KIND,
    MORNING_BRIEF_RESUME_ID,
    NLM_WIKI_SYNC_JOB_KIND,
    NLM_WIKI_SYNC_RESUME_KEY,
    NOTEBOOKLM_ORCHESTRATION_POLL_JOB_KIND,
    NOTEBOOKLM_ORCHESTRATION_POLL_RESUME_KEY,
    PERF_OPTIMIZER_JOB_KIND,
    PERF_OPTIMIZER_RESUME_KEY,
    WIKI_RESEARCH_JOB_KIND,
    WIKI_RESEARCH_RESUME_KEY,
    WIKI_SCRAPE_JOB_KIND,
    WIKI_SCRAPE_RESUME_KEY,
    ScheduledBackgroundJobRunner,
    enqueue_scheduled_background_job,
    kairos_tick_result_summary,
    nlm_wiki_sync_result_summary,
    notebooklm_orchestration_poll_result_summary,
    safe_non_negative_int,
    wiki_research_result_summary,
    wiki_scrape_result_summary,
)


class ScheduledBackgroundJobTests(unittest.TestCase):
    def test_scheduled_job_kinds_have_isolated_resume_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs = JobService(Path(tmpdir) / "claw.db")

            poll_first = enqueue_scheduled_background_job(
                job_name="notebooklm_orchestration_poll",
                job_kind=NOTEBOOKLM_ORCHESTRATION_POLL_JOB_KIND,
                resume_key=NOTEBOOKLM_ORCHESTRATION_POLL_RESUME_KEY,
                job_service=jobs,
                payload={"limit": 3},
            )
            poll_second = enqueue_scheduled_background_job(
                job_name="notebooklm_orchestration_poll",
                job_kind=NOTEBOOKLM_ORCHESTRATION_POLL_JOB_KIND,
                resume_key=NOTEBOOKLM_ORCHESTRATION_POLL_RESUME_KEY,
                job_service=jobs,
                payload={"limit": 3},
            )
            sync_first = enqueue_scheduled_background_job(
                job_name="nlm_wiki_sync",
                job_kind=NLM_WIKI_SYNC_JOB_KIND,
                resume_key=NLM_WIKI_SYNC_RESUME_KEY,
                job_service=jobs,
            )
            sync_second = enqueue_scheduled_background_job(
                job_name="nlm_wiki_sync",
                job_kind=NLM_WIKI_SYNC_JOB_KIND,
                resume_key=NLM_WIKI_SYNC_RESUME_KEY,
                job_service=jobs,
            )
            morning_first = enqueue_scheduled_background_job(
                job_name="morning_brief",
                job_kind=MORNING_BRIEF_JOB_KIND,
                resume_key=MORNING_BRIEF_RESUME_ID,
                job_service=jobs,
            )
            morning_second = enqueue_scheduled_background_job(
                job_name="morning_brief",
                job_kind=MORNING_BRIEF_JOB_KIND,
                resume_key=MORNING_BRIEF_RESUME_ID,
                job_service=jobs,
            )
            evening_first = enqueue_scheduled_background_job(
                job_name="evening_brief",
                job_kind=EVENING_BRIEF_JOB_KIND,
                resume_key=EVENING_BRIEF_RESUME_ID,
                job_service=jobs,
            )
            evening_second = enqueue_scheduled_background_job(
                job_name="evening_brief",
                job_kind=EVENING_BRIEF_JOB_KIND,
                resume_key=EVENING_BRIEF_RESUME_ID,
                job_service=jobs,
            )

            self.assertEqual(poll_first, poll_second)
            self.assertEqual(sync_first, sync_second)
            self.assertEqual(morning_first, morning_second)
            self.assertEqual(evening_first, evening_second)
            self.assertNotEqual(poll_first, sync_first)
            self.assertNotEqual(morning_first, evening_first)
            self.assertNotEqual(poll_first, morning_first)
            self.assertEqual(
                len(jobs.list(kinds=(NOTEBOOKLM_ORCHESTRATION_POLL_JOB_KIND,), limit=10)),
                1,
            )
            self.assertEqual(len(jobs.list(kinds=(NLM_WIKI_SYNC_JOB_KIND,), limit=10)), 1)
            self.assertEqual(len(jobs.list(kinds=(MORNING_BRIEF_JOB_KIND,), limit=10)), 1)
            self.assertEqual(len(jobs.list(kinds=(EVENING_BRIEF_JOB_KIND,), limit=10)), 1)

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

    def test_run_once_closes_jobs_under_formal_leases(self) -> None:
        # Invariant (lease-guard P1): with formal_leases_enabled, run_once must
        # propagate the claimed JobRecord's lease credentials to complete()/
        # fail() so the durable row leaves 'running'. Without propagation the
        # guard returns None silently and the row stays 'running' while
        # *_job_completed/_failed events are still emitted.
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs = JobService(
                Path(tmpdir) / "claw.db",
                formal_leases_enabled=True,
            )

            enqueue_scheduled_background_job(
                job_name="wiki_research",
                job_kind=WIKI_RESEARCH_JOB_KIND,
                resume_key=WIKI_RESEARCH_RESUME_KEY,
                job_service=jobs,
            )
            success_runner = ScheduledBackgroundJobRunner(
                job_name="wiki_research",
                job_kind=WIKI_RESEARCH_JOB_KIND,
                job_service=jobs,
                handler=MagicMock(return_value={"ok": True}),
            )
            self.assertTrue(success_runner.run_once())
            completed = jobs.list(kinds=(WIKI_RESEARCH_JOB_KIND,), limit=10)[0]
            self.assertEqual(completed.status, "completed")
            self.assertIsNone(completed.lease_owner)

            enqueue_scheduled_background_job(
                job_name="wiki_scrape",
                job_kind=WIKI_SCRAPE_JOB_KIND,
                resume_key=WIKI_SCRAPE_RESUME_KEY,
                job_service=jobs,
            )
            failure_runner = ScheduledBackgroundJobRunner(
                job_name="wiki_scrape",
                job_kind=WIKI_SCRAPE_JOB_KIND,
                job_service=jobs,
                handler=MagicMock(side_effect=RuntimeError("boom")),
            )
            self.assertTrue(failure_runner.run_once())
            failed = jobs.list(kinds=(WIKI_SCRAPE_JOB_KIND,), limit=10)[0]
            self.assertEqual(failed.status, "retrying")
            self.assertIsNone(failed.lease_owner)

    def test_run_once_emits_lease_lost_when_close_does_not_land(self) -> None:
        # D2 rollout: when the close was not ours (lease stolen mid-execution),
        # emit {job_name}_job_lease_lost and suppress the lying completed
        # event (jobs.close_landed; pattern from #242/#243).
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = MagicMock()
            jobs = JobService(
                Path(tmpdir) / "claw.db",
                formal_leases_enabled=True,
            )
            enqueue_scheduled_background_job(
                job_name="wiki_research",
                job_kind=WIKI_RESEARCH_JOB_KIND,
                resume_key=WIKI_RESEARCH_RESUME_KEY,
                job_service=jobs,
            )

            def steal_lease(payload):
                jobs.reclaim_expired_leases(
                    kinds=(WIKI_RESEARCH_JOB_KIND,),
                    now=time.time() + 100_000,
                )
                stolen = jobs.claim_next(
                    worker_id="thief",
                    kinds=(WIKI_RESEARCH_JOB_KIND,),
                    now=time.time() + 100_001,
                )
                assert stolen is not None
                return {"ok": True}

            runner = ScheduledBackgroundJobRunner(
                job_name="wiki_research",
                job_kind=WIKI_RESEARCH_JOB_KIND,
                job_service=jobs,
                handler=steal_lease,
                observe=observe,
            )

            self.assertTrue(runner.run_once())

            emitted = [call.args[0] for call in observe.emit.call_args_list]
            self.assertIn("wiki_research_job_lease_lost", emitted)
            self.assertNotIn("wiki_research_job_completed", emitted)
            job = jobs.list(kinds=(WIKI_RESEARCH_JOB_KIND,), limit=10)[0]
            self.assertEqual(job.status, "running")
            self.assertEqual(job.lease_owner, "thief")

    def test_wiki_scrape_result_summary_keeps_bounded_source_diagnostics(self) -> None:
        source_results = [
            {
                "source": f"Source {idx}",
                "url": f"https://example.com/{idx}",
                "status": "scraped",
                "items_extracted": idx,
                "items_ingested": 0,
                "items_skipped": 2,
                "skip_reasons": {"duplicate": 1, "body_too_short": 1},
                "raw_body": "must not persist",
            }
            for idx in range(12)
        ]
        source_results[0]["source"] = "Source A"
        item_results = [
            {
                "source": "Source A",
                "title": "Duplicate Topic " + ("x" * 200),
                "slug": f"duplicate-topic-{idx}",
                "status": "skipped",
                "reason": "duplicate",
                "body": "must not persist",
            }
            for idx in range(25)
        ]
        result = wiki_scrape_result_summary(
            {
                "sources_scraped": 8,
                "pages_ingested": 0,
                "sources_skipped": 0,
                "source_results": source_results,
                "item_results": item_results,
            }
        )

        self.assertEqual(result["sources_scraped"], 8)
        self.assertEqual(result["pages_ingested"], 0)
        self.assertEqual(result["sources_skipped"], 0)
        self.assertEqual(len(result["source_results"]), 10)
        self.assertEqual(len(result["item_results"]), 20)
        self.assertEqual(result["source_results"][0]["source"], "Source A")
        self.assertEqual(result["source_results"][0]["skip_reasons"]["duplicate"], 1)
        self.assertNotIn("raw_body", result["source_results"][0])
        self.assertEqual(result["item_results"][0]["reason"], "duplicate")
        self.assertLessEqual(len(result["item_results"][0]["title"]), 123)
        self.assertTrue(result["item_results"][0]["title"].endswith("..."))
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

    def test_notebooklm_result_summaries_are_bounded(self) -> None:
        poll_summary = notebooklm_orchestration_poll_result_summary(2)
        self.assertEqual(poll_summary, {"processed": 2})

        sync_summary = nlm_wiki_sync_result_summary(
            {
                "notebooks_scanned": 3,
                "pages_written": 2,
                "raw_response": "must not persist",
            }
        )
        self.assertEqual(sync_summary, {"notebooks_scanned": 3, "pages_written": 2})

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

    def test_reclaim_stale_running_delegates_to_lease_reclaim_under_formal_leases(
        self,
    ) -> None:
        # Invariant (A1): with formal_leases_enabled the age-based reclaim is
        # replaced by the lease-native reclaim — this runner is not the lease
        # owner, so its credential-less fail() could never pass the guard and
        # would emit lying *_job_stale_reclaimed events. Two sides locked:
        # an UNEXPIRED lease is never stolen (even past stale_running_seconds),
        # and an EXPIRED lease IS recovered (no daemon lane covers scheduler.*
        # kinds, so the runner must own its recovery).
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = MagicMock()
            jobs = JobService(
                Path(tmpdir) / "claw.db",
                formal_leases_enabled=True,
                default_lease_seconds=30,
            )
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
                handler=MagicMock(return_value=None),
                observe=observe,
                stale_running_seconds=1,
            )

            # Side 1: lease still valid (age 2s >= stale_running_seconds=1,
            # but TTL 30s not reached) — must NOT be stolen.
            self.assertEqual(runner.reclaim_stale_running(now=claimed_at + 2), 0)
            held = jobs.get(stuck.job_id)
            self.assertEqual(held.status, "running")
            self.assertEqual(held.lease_owner, "dead-worker")
            stale_events = [
                call
                for call in observe.emit.call_args_list
                if call.args[0] == "perf_optimizer_job_stale_reclaimed"
            ]
            self.assertEqual(stale_events, [])

            # Side 2: lease expired (past TTL 30s) — recovery must happen.
            self.assertEqual(runner.reclaim_stale_running(now=claimed_at + 60), 1)
            recovered = jobs.get(stuck.job_id)
            self.assertEqual(recovered.status, "retrying")
            self.assertIsNone(recovered.lease_owner)

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

    def test_runner_timeout_fails_observably_without_waiting_for_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = MagicMock()
            jobs = JobService(Path(tmpdir) / "claw.db")

            def blocking_handler(_payload: dict) -> int:
                time.sleep(0.2)
                return 1

            enqueue_scheduled_background_job(
                job_name="notebooklm_orchestration_poll",
                job_kind=NOTEBOOKLM_ORCHESTRATION_POLL_JOB_KIND,
                resume_key=NOTEBOOKLM_ORCHESTRATION_POLL_RESUME_KEY,
                job_service=jobs,
                max_attempts=1,
            )
            runner = ScheduledBackgroundJobRunner(
                job_name="notebooklm_orchestration_poll",
                job_kind=NOTEBOOKLM_ORCHESTRATION_POLL_JOB_KIND,
                job_service=jobs,
                handler=blocking_handler,
                observe=observe,
                timeout_seconds=0.01,
            )

            started = time.monotonic()
            self.assertTrue(runner.run_once())
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.15)
            job = jobs.list(kinds=(NOTEBOOKLM_ORCHESTRATION_POLL_JOB_KIND,), limit=10)[0]
            self.assertEqual(job.status, "failed")
            self.assertIn("timed out", job.error)
            failed_events = [
                call.kwargs["payload"]
                for call in observe.emit.call_args_list
                if call.args[0] == "notebooklm_orchestration_poll_job_failed"
            ]
            self.assertEqual(len(failed_events), 1)
            self.assertEqual(failed_events[0]["error_type"], "TimeoutError")

    def test_runner_timeout_is_single_flight_until_prior_handler_returns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs = JobService(Path(tmpdir) / "claw.db")
            started = threading.Event()
            release = threading.Event()
            call_count = 0

            def blocking_handler(_payload: dict) -> int:
                nonlocal call_count
                call_count += 1
                started.set()
                release.wait(timeout=1.0)
                return 1

            runner = ScheduledBackgroundJobRunner(
                job_name="notebooklm_orchestration_poll",
                job_kind=NOTEBOOKLM_ORCHESTRATION_POLL_JOB_KIND,
                job_service=jobs,
                handler=blocking_handler,
                timeout_seconds=0.01,
            )
            try:
                enqueue_scheduled_background_job(
                    job_name="notebooklm_orchestration_poll",
                    job_kind=NOTEBOOKLM_ORCHESTRATION_POLL_JOB_KIND,
                    resume_key=NOTEBOOKLM_ORCHESTRATION_POLL_RESUME_KEY,
                    job_service=jobs,
                    max_attempts=1,
                )

                self.assertTrue(runner.run_once())
                self.assertTrue(started.wait(timeout=0.1))
                self.assertEqual(call_count, 1)
                enqueue_scheduled_background_job(
                    job_name="notebooklm_orchestration_poll",
                    job_kind=NOTEBOOKLM_ORCHESTRATION_POLL_JOB_KIND,
                    resume_key=NOTEBOOKLM_ORCHESTRATION_POLL_RESUME_KEY,
                    job_service=jobs,
                    max_attempts=1,
                )

                self.assertTrue(runner.run_once())

                self.assertEqual(call_count, 1)
                rows = jobs.list(kinds=(NOTEBOOKLM_ORCHESTRATION_POLL_JOB_KIND,), limit=10)
                self.assertEqual([row.status for row in rows], ["failed", "failed"])
                self.assertTrue(
                    any("still running after a prior timeout" in row.error for row in rows)
                )
            finally:
                release.set()


class ScheduledBackgroundRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def _brief_runtime(self, tmpdir: str, *, timeout_seconds: float = 0.5):
        from claw_v2.lifecycle import wire_brief_scheduler_jobs

        root = Path(tmpdir)
        observe = MagicMock()
        job_service = JobService(root / "claw.db")
        scheduler = CronScheduler()
        registered: dict[str, object] = {}

        class _Daemon:
            def register_background_job_runner(self, *, name, handler, interval=60.0):
                registered[name] = SimpleNamespace(name=name, handler=handler, interval=interval)

        runtime = SimpleNamespace(
            scheduler=scheduler,
            job_service=job_service,
            observe=observe,
            daemon=_Daemon(),
        )
        morning_brief = SimpleNamespace(
            settings=SimpleNamespace(timezone="America/Chicago"),
            run_if_due=MagicMock(return_value="morning ready"),
        )
        evening_brief = SimpleNamespace(
            settings=SimpleNamespace(timezone="America/Chicago"),
            run_if_due=MagicMock(return_value="evening ready"),
        )
        wire_brief_scheduler_jobs(
            runtime,
            morning_brief=morning_brief,
            evening_brief=evening_brief,
            timeout_seconds=timeout_seconds,
        )
        return runtime, morning_brief, evening_brief, registered

    def _real_brief_service(
        self,
        *,
        stamp_path: Path,
        sent: list[str],
        hour: int,
        report_name: str,
        delayed_now: datetime,
    ) -> MorningBriefService:
        return MorningBriefService(
            settings=MorningBriefSettings(
                hour=hour,
                timezone="America/Chicago",
                stamp_path=stamp_path,
                report_name=report_name,
                greeting="Cierre del dia, Hector."
                if report_name == "evening_brief"
                else "Buenos dias, Hector.",
            ),
            notify=sent.append,
            clock=lambda: delayed_now,
            weather_fetcher=lambda location, timeout: "auto: 70F",
            calendar_fetcher=lambda timeout: "sin eventos",
            email_fetcher=lambda timeout: "sin correo",
        )

    def _notebooklm_runtime(
        self,
        tmpdir: str,
        *,
        timeout_seconds: float = 0.5,
        wiki_available: bool = True,
    ):
        from claw_v2.lifecycle import wire_notebooklm_scheduler_jobs

        root = Path(tmpdir)
        observe = MagicMock()
        job_service = JobService(root / "claw.db")
        scheduler = CronScheduler()
        registered: dict[str, object] = {}

        class _Daemon:
            def register_background_job_runner(self, *, name, handler, interval=60.0):
                registered[name] = SimpleNamespace(name=name, handler=handler, interval=interval)

        wiki = SimpleNamespace(ingest_from_notebooklm=MagicMock()) if wiki_available else None
        runtime = SimpleNamespace(
            scheduler=scheduler,
            job_service=job_service,
            observe=observe,
            daemon=_Daemon(),
            config=SimpleNamespace(notebooklm_cli_long_timeout_seconds=timeout_seconds),
            bot=SimpleNamespace(wiki=wiki),
            kairos=SimpleNamespace(),
        )
        nlm_service = SimpleNamespace(poll_orchestrations=MagicMock())
        wire_notebooklm_scheduler_jobs(runtime, nlm_service)
        return runtime, nlm_service, wiki, registered

    def test_lifecycle_notebooklm_handlers_enqueue_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime, nlm_service, wiki, registered = self._notebooklm_runtime(tmpdir)
            jobs = {job.name: job for job in runtime.scheduler.list_jobs()}
            self.assertIn("notebooklm_orchestration_poll", jobs)
            self.assertIn("nlm_wiki_sync", jobs)
            self.assertEqual(jobs["notebooklm_orchestration_poll"].interval_seconds, 60)
            self.assertEqual(jobs["nlm_wiki_sync"].interval_seconds, 43200)
            self.assertIn("notebooklm_orchestration_poll", registered)
            self.assertIn("nlm_wiki_sync", registered)

            started = time.monotonic()
            jobs["notebooklm_orchestration_poll"].handler()
            jobs["nlm_wiki_sync"].handler()
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.1)
            nlm_service.poll_orchestrations.assert_not_called()
            wiki.ingest_from_notebooklm.assert_not_called()
            poll_rows = runtime.job_service.list(
                kinds=(NOTEBOOKLM_ORCHESTRATION_POLL_JOB_KIND,), limit=10
            )
            sync_rows = runtime.job_service.list(kinds=(NLM_WIKI_SYNC_JOB_KIND,), limit=10)
            self.assertEqual(len(poll_rows), 1)
            self.assertEqual(poll_rows[0].status, "queued")
            self.assertEqual(poll_rows[0].payload["limit"], 3)
            self.assertEqual(len(sync_rows), 1)
            self.assertEqual(sync_rows[0].status, "queued")

    def test_lifecycle_notebooklm_kairos_service_assigned_without_wiki(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime, nlm_service, wiki, registered = self._notebooklm_runtime(
                tmpdir,
                wiki_available=False,
            )
            jobs = {job.name: job for job in runtime.scheduler.list_jobs()}

            self.assertIsNone(wiki)
            self.assertIs(runtime.kairos.nlm_service, nlm_service)
            self.assertIn("notebooklm_orchestration_poll", jobs)
            self.assertNotIn("nlm_wiki_sync", jobs)
            self.assertIn("notebooklm_orchestration_poll", registered)
            self.assertNotIn("nlm_wiki_sync", registered)

    def test_lifecycle_notebooklm_runners_execute_body_off_tick(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime, nlm_service, wiki, registered = self._notebooklm_runtime(tmpdir)
            nlm_service.poll_orchestrations.return_value = 2
            wiki.ingest_from_notebooklm.return_value = {
                "notebooks_scanned": 1,
                "pages_written": 1,
            }
            jobs = {job.name: job for job in runtime.scheduler.list_jobs()}
            jobs["notebooklm_orchestration_poll"].handler()
            jobs["nlm_wiki_sync"].handler()

            self.assertEqual(registered["notebooklm_orchestration_poll"].handler(), 1)
            self.assertEqual(registered["nlm_wiki_sync"].handler(), 1)

            nlm_service.poll_orchestrations.assert_called_once_with(limit=3)
            wiki.ingest_from_notebooklm.assert_called_once_with(
                nlm_service,
                max_notebooks=3,
                questions_per_nb=2,
            )
            poll_rows = runtime.job_service.list(
                kinds=(NOTEBOOKLM_ORCHESTRATION_POLL_JOB_KIND,), limit=10
            )
            sync_rows = runtime.job_service.list(kinds=(NLM_WIKI_SYNC_JOB_KIND,), limit=10)
            self.assertEqual(poll_rows[0].status, "completed")
            self.assertEqual(poll_rows[0].result, {"processed": 2})
            self.assertEqual(sync_rows[0].status, "completed")
            self.assertEqual(sync_rows[0].result, {"notebooks_scanned": 1, "pages_written": 1})

    def test_lifecycle_notebooklm_runner_timeout_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime, nlm_service, _wiki, registered = self._notebooklm_runtime(
                tmpdir, timeout_seconds=0.01
            )

            def blocked_poll(*, limit: int) -> int:
                time.sleep(0.2)
                return limit

            nlm_service.poll_orchestrations.side_effect = blocked_poll
            jobs = {job.name: job for job in runtime.scheduler.list_jobs()}
            jobs["notebooklm_orchestration_poll"].handler()

            started = time.monotonic()
            self.assertEqual(registered["notebooklm_orchestration_poll"].handler(), 1)
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.15)
            rows = runtime.job_service.list(
                kinds=(NOTEBOOKLM_ORCHESTRATION_POLL_JOB_KIND,), limit=10
            )
            self.assertEqual(rows[0].status, "failed")
            self.assertIn("timed out", rows[0].error)
            failed_events = [
                call.kwargs["payload"]
                for call in runtime.observe.emit.call_args_list
                if call.args[0] == "notebooklm_orchestration_poll_job_failed"
            ]
            self.assertEqual(len(failed_events), 1)
            self.assertEqual(failed_events[0]["error_type"], "TimeoutError")

    def test_lifecycle_brief_handlers_enqueue_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime, morning_brief, evening_brief, registered = self._brief_runtime(tmpdir)
            jobs = {job.name: job for job in runtime.scheduler.list_jobs()}
            self.assertIn("morning_brief", jobs)
            self.assertIn("evening_brief", jobs)
            self.assertEqual(jobs["morning_brief"].interval_seconds, 300)
            self.assertEqual(jobs["evening_brief"].interval_seconds, 300)
            self.assertIn("morning_brief", registered)
            self.assertIn("evening_brief", registered)

            started = time.monotonic()
            jobs["morning_brief"].handler()
            jobs["evening_brief"].handler()
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.1)
            morning_brief.run_if_due.assert_not_called()
            evening_brief.run_if_due.assert_not_called()
            morning_rows = runtime.job_service.list(kinds=(MORNING_BRIEF_JOB_KIND,), limit=10)
            evening_rows = runtime.job_service.list(kinds=(EVENING_BRIEF_JOB_KIND,), limit=10)
            self.assertEqual(len(morning_rows), 1)
            self.assertEqual(morning_rows[0].status, "queued")
            self.assertEqual(morning_rows[0].resume_key, MORNING_BRIEF_RESUME_ID)
            self.assertEqual(len(evening_rows), 1)
            self.assertEqual(evening_rows[0].status, "queued")
            self.assertEqual(evening_rows[0].resume_key, EVENING_BRIEF_RESUME_ID)

    def test_lifecycle_brief_runners_execute_body_off_tick(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime, morning_brief, evening_brief, registered = self._brief_runtime(tmpdir)
            jobs = {job.name: job for job in runtime.scheduler.list_jobs()}
            jobs["morning_brief"].handler()
            jobs["evening_brief"].handler()

            self.assertEqual(registered["morning_brief"].handler(), 1)
            self.assertEqual(registered["evening_brief"].handler(), 1)

            morning_brief.run_if_due.assert_called_once()
            evening_brief.run_if_due.assert_called_once()
            self.assertIn("now", morning_brief.run_if_due.call_args.kwargs)
            self.assertIn("now", evening_brief.run_if_due.call_args.kwargs)
            morning_rows = runtime.job_service.list(kinds=(MORNING_BRIEF_JOB_KIND,), limit=10)
            evening_rows = runtime.job_service.list(kinds=(EVENING_BRIEF_JOB_KIND,), limit=10)
            self.assertEqual(morning_rows[0].status, "completed")
            self.assertEqual(morning_rows[0].result, {"sent": True, "message_chars": 13})
            self.assertEqual(evening_rows[0].status, "completed")
            self.assertEqual(evening_rows[0].result, {"sent": True, "message_chars": 13})

    def test_lifecycle_morning_brief_runner_uses_enqueue_time_for_due_window(self) -> None:
        from claw_v2.lifecycle import wire_brief_scheduler_jobs

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sent: list[str] = []
            runtime = SimpleNamespace(
                scheduler=CronScheduler(),
                job_service=JobService(root / "claw.db"),
                observe=MagicMock(),
                daemon=SimpleNamespace(),
            )
            registered: dict[str, object] = {}
            runtime.daemon.register_background_job_runner = lambda *, name, handler, interval=60.0: (
                registered.setdefault(
                    name,
                    SimpleNamespace(name=name, handler=handler, interval=interval),
                )
            )
            due_time = datetime(2026, 4, 27, 5, 59, tzinfo=ZoneInfo("America/Chicago"))
            delayed_now = datetime(2026, 4, 27, 6, 5, tzinfo=ZoneInfo("America/Chicago"))
            morning_brief = self._real_brief_service(
                stamp_path=root / "morning.txt",
                sent=sent,
                hour=5,
                report_name="morning_brief",
                delayed_now=delayed_now,
            )
            evening_brief = self._real_brief_service(
                stamp_path=root / "evening.txt",
                sent=[],
                hour=21,
                report_name="evening_brief",
                delayed_now=delayed_now,
            )
            wire_brief_scheduler_jobs(
                runtime,
                morning_brief=morning_brief,
                evening_brief=evening_brief,
            )
            jobs = {job.name: job for job in runtime.scheduler.list_jobs()}

            with patch(
                "claw_v2.scheduled_background_jobs.time.time", return_value=due_time.timestamp()
            ):
                jobs["morning_brief"].handler()

            self.assertEqual(registered["morning_brief"].handler(), 1)
            rows = runtime.job_service.list(kinds=(MORNING_BRIEF_JOB_KIND,), limit=10)
            self.assertEqual(rows[0].status, "completed")
            self.assertEqual(rows[0].result["sent"], True)
            self.assertEqual(len(sent), 1)
            self.assertEqual((root / "morning.txt").read_text(encoding="utf-8"), "2026-04-27")

    def test_lifecycle_evening_brief_runner_uses_enqueue_time_for_due_window(self) -> None:
        from claw_v2.lifecycle import wire_brief_scheduler_jobs

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sent: list[str] = []
            runtime = SimpleNamespace(
                scheduler=CronScheduler(),
                job_service=JobService(root / "claw.db"),
                observe=MagicMock(),
                daemon=SimpleNamespace(),
            )
            registered: dict[str, object] = {}
            runtime.daemon.register_background_job_runner = lambda *, name, handler, interval=60.0: (
                registered.setdefault(
                    name,
                    SimpleNamespace(name=name, handler=handler, interval=interval),
                )
            )
            due_time = datetime(2026, 4, 27, 21, 59, tzinfo=ZoneInfo("America/Chicago"))
            delayed_now = datetime(2026, 4, 27, 22, 5, tzinfo=ZoneInfo("America/Chicago"))
            morning_brief = self._real_brief_service(
                stamp_path=root / "morning.txt",
                sent=[],
                hour=5,
                report_name="morning_brief",
                delayed_now=delayed_now,
            )
            evening_brief = self._real_brief_service(
                stamp_path=root / "evening.txt",
                sent=sent,
                hour=21,
                report_name="evening_brief",
                delayed_now=delayed_now,
            )
            wire_brief_scheduler_jobs(
                runtime,
                morning_brief=morning_brief,
                evening_brief=evening_brief,
            )
            jobs = {job.name: job for job in runtime.scheduler.list_jobs()}

            with patch(
                "claw_v2.scheduled_background_jobs.time.time", return_value=due_time.timestamp()
            ):
                jobs["evening_brief"].handler()

            self.assertEqual(registered["evening_brief"].handler(), 1)
            rows = runtime.job_service.list(kinds=(EVENING_BRIEF_JOB_KIND,), limit=10)
            self.assertEqual(rows[0].status, "completed")
            self.assertEqual(rows[0].result["sent"], True)
            self.assertEqual(len(sent), 1)
            self.assertEqual((root / "evening.txt").read_text(encoding="utf-8"), "2026-04-27")

    def test_lifecycle_morning_brief_runner_rejects_enqueue_time_outside_due_window(
        self,
    ) -> None:
        from claw_v2.lifecycle import wire_brief_scheduler_jobs

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sent: list[str] = []
            runtime = SimpleNamespace(
                scheduler=CronScheduler(),
                job_service=JobService(root / "claw.db"),
                observe=MagicMock(),
                daemon=SimpleNamespace(),
            )
            registered: dict[str, object] = {}
            runtime.daemon.register_background_job_runner = lambda *, name, handler, interval=60.0: (
                registered.setdefault(
                    name,
                    SimpleNamespace(name=name, handler=handler, interval=interval),
                )
            )
            enqueue_time = datetime(2026, 4, 27, 4, 59, tzinfo=ZoneInfo("America/Chicago"))
            delayed_now = datetime(2026, 4, 27, 5, 5, tzinfo=ZoneInfo("America/Chicago"))
            morning_brief = self._real_brief_service(
                stamp_path=root / "morning.txt",
                sent=sent,
                hour=5,
                report_name="morning_brief",
                delayed_now=delayed_now,
            )
            evening_brief = self._real_brief_service(
                stamp_path=root / "evening.txt",
                sent=[],
                hour=21,
                report_name="evening_brief",
                delayed_now=delayed_now,
            )
            wire_brief_scheduler_jobs(
                runtime,
                morning_brief=morning_brief,
                evening_brief=evening_brief,
            )
            jobs = {job.name: job for job in runtime.scheduler.list_jobs()}

            with patch(
                "claw_v2.scheduled_background_jobs.time.time",
                return_value=enqueue_time.timestamp(),
            ):
                jobs["morning_brief"].handler()

            self.assertEqual(registered["morning_brief"].handler(), 1)
            rows = runtime.job_service.list(kinds=(MORNING_BRIEF_JOB_KIND,), limit=10)
            self.assertEqual(rows[0].status, "completed")
            self.assertEqual(rows[0].result, {"sent": False, "message_chars": 0})
            self.assertEqual(sent, [])
            self.assertFalse((root / "morning.txt").exists())

    def test_lifecycle_evening_brief_runner_rejects_enqueue_time_outside_due_window(
        self,
    ) -> None:
        from claw_v2.lifecycle import wire_brief_scheduler_jobs

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sent: list[str] = []
            runtime = SimpleNamespace(
                scheduler=CronScheduler(),
                job_service=JobService(root / "claw.db"),
                observe=MagicMock(),
                daemon=SimpleNamespace(),
            )
            registered: dict[str, object] = {}
            runtime.daemon.register_background_job_runner = lambda *, name, handler, interval=60.0: (
                registered.setdefault(
                    name,
                    SimpleNamespace(name=name, handler=handler, interval=interval),
                )
            )
            enqueue_time = datetime(2026, 4, 27, 20, 59, tzinfo=ZoneInfo("America/Chicago"))
            delayed_now = datetime(2026, 4, 27, 21, 5, tzinfo=ZoneInfo("America/Chicago"))
            morning_brief = self._real_brief_service(
                stamp_path=root / "morning.txt",
                sent=[],
                hour=5,
                report_name="morning_brief",
                delayed_now=delayed_now,
            )
            evening_brief = self._real_brief_service(
                stamp_path=root / "evening.txt",
                sent=sent,
                hour=21,
                report_name="evening_brief",
                delayed_now=delayed_now,
            )
            wire_brief_scheduler_jobs(
                runtime,
                morning_brief=morning_brief,
                evening_brief=evening_brief,
            )
            jobs = {job.name: job for job in runtime.scheduler.list_jobs()}

            with patch(
                "claw_v2.scheduled_background_jobs.time.time",
                return_value=enqueue_time.timestamp(),
            ):
                jobs["evening_brief"].handler()

            self.assertEqual(registered["evening_brief"].handler(), 1)
            rows = runtime.job_service.list(kinds=(EVENING_BRIEF_JOB_KIND,), limit=10)
            self.assertEqual(rows[0].status, "completed")
            self.assertEqual(rows[0].result, {"sent": False, "message_chars": 0})
            self.assertEqual(sent, [])
            self.assertFalse((root / "evening.txt").exists())

    def test_lifecycle_brief_duplicate_ticks_do_not_fan_out_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime, _morning_brief, _evening_brief, _registered = self._brief_runtime(tmpdir)
            jobs = {job.name: job for job in runtime.scheduler.list_jobs()}

            jobs["morning_brief"].handler()
            jobs["morning_brief"].handler()
            jobs["evening_brief"].handler()
            jobs["evening_brief"].handler()

            morning_rows = runtime.job_service.list(kinds=(MORNING_BRIEF_JOB_KIND,), limit=10)
            evening_rows = runtime.job_service.list(kinds=(EVENING_BRIEF_JOB_KIND,), limit=10)
            self.assertEqual(len(morning_rows), 1)
            self.assertEqual(len(evening_rows), 1)
            self.assertEqual(morning_rows[0].status, "queued")
            self.assertEqual(evening_rows[0].status, "queued")

    def test_lifecycle_brief_runner_timeout_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime, morning_brief, _evening_brief, registered = self._brief_runtime(
                tmpdir,
                timeout_seconds=0.01,
            )
            release = threading.Event()

            def blocked_brief(**_kwargs: object) -> str:
                release.wait(timeout=2.0)
                return "late"

            morning_brief.run_if_due.side_effect = blocked_brief
            jobs = {job.name: job for job in runtime.scheduler.list_jobs()}
            jobs["morning_brief"].handler()

            started = time.monotonic()
            try:
                self.assertEqual(registered["morning_brief"].handler(), 1)
                elapsed = time.monotonic() - started

                self.assertLess(elapsed, 0.15)
                rows = runtime.job_service.list(kinds=(MORNING_BRIEF_JOB_KIND,), limit=10)
                self.assertEqual(rows[0].status, "failed")
                self.assertIn("timed out", rows[0].error)
                failed_events = [
                    call.kwargs["payload"]
                    for call in runtime.observe.emit.call_args_list
                    if call.args[0] == "morning_brief_job_failed"
                ]
                self.assertEqual(len(failed_events), 1)
                self.assertEqual(failed_events[0]["error_type"], "TimeoutError")
            finally:
                release.set()

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
                "CLAW_BRANCH_INTEGRITY_CHECK": "0",
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
                "CLAW_BRANCH_INTEGRITY_CHECK": "0",
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
