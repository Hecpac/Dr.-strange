"""Tests for NotebookLMResearchRunner (Phase 3, Tasks 3.2-3.4)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.f2_durability_store import F2DurabilityStore
from claw_v2.jobs import JobService
from claw_v2.notebooklm_research_runner import NotebookLMResearchRunner
from claw_v2.sqlite_runtime import RuntimeDb


def _setup(tmp: str) -> tuple[JobService, F2DurabilityStore]:
    db_path = Path(tmp) / "claw.db"
    runtime_db = RuntimeDb(db_path)
    jobs = JobService(db_path, runtime_db=runtime_db)
    store = F2DurabilityStore(runtime_db)
    return jobs, store


class RunnerHappyTests(unittest.TestCase):
    def test_run_once_completes_job_and_records_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jobs, store = _setup(tmp)
            job = jobs.enqueue(
                kind="notebooklm.research",
                payload={"notebook_id": "nb-1", "query": "q", "mode": "deep"},
            )
            runner = NotebookLMResearchRunner(
                job_service=jobs,
                store=store,
                observe=None,
                notifier=None,
                deep_research_fn=lambda nb, q: 3,
                status_fn=lambda nb: {"source_count": 0},
            )
            ran = runner.run_once()
            self.assertTrue(ran)
            record = jobs.get(job.job_id)
            self.assertEqual(record.status, "completed")
            effects = store.list_external_effects()
            self.assertEqual(len(effects), 1)
            self.assertEqual(effects[0].status, "applied")

    def test_run_once_returns_false_when_no_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jobs, store = _setup(tmp)
            runner = NotebookLMResearchRunner(
                job_service=jobs,
                store=store,
                observe=None,
                notifier=None,
                deep_research_fn=lambda nb, q: 1,
                status_fn=lambda nb: {"source_count": 0},
            )
            ran = runner.run_once()
            self.assertFalse(ran)


class RunnerBlockedTests(unittest.TestCase):
    def test_blocked_fails_job_no_retry_emits_observe_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jobs, store = _setup(tmp)
            job = jobs.enqueue(
                kind="notebooklm.research",
                payload={"notebook_id": "nb-1", "query": "q", "mode": "deep"},
            )

            observe_events: list[str] = []

            class FakeObserve:
                def emit(self, event_type: str, payload: object = None) -> None:
                    observe_events.append(event_type)

            runner = NotebookLMResearchRunner(
                job_service=jobs,
                store=store,
                observe=FakeObserve(),
                notifier=None,
                deep_research_fn=lambda nb, q: 0,  # zero imports → blocked
                status_fn=lambda nb: {"source_count": 0},
            )
            ran = runner.run_once()
            self.assertTrue(ran)

            record = jobs.get(job.job_id)
            self.assertEqual(record.status, "failed")
            self.assertEqual(record.error, "effect_blocked_manual_review")

            # retry=False means no retrying status
            self.assertNotEqual(record.status, "retrying")

            # observe event emitted
            self.assertIn("notebooklm_research_effect_blocked_manual_review", observe_events)

            effects = store.list_external_effects()
            self.assertEqual(len(effects), 1)
            self.assertEqual(effects[0].status, "blocked_manual_review")

    def test_blocked_notifies_telegram_if_notifier_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jobs, store = _setup(tmp)
            jobs.enqueue(
                kind="notebooklm.research",
                payload={"notebook_id": "nb-2", "query": "test", "mode": "deep"},
            )

            notifier = MagicMock()
            runner = NotebookLMResearchRunner(
                job_service=jobs,
                store=store,
                observe=None,
                notifier=notifier,
                deep_research_fn=lambda nb, q: 0,
                status_fn=lambda nb: {"source_count": 0},
            )
            runner.run_once()
            notifier.assert_called_once()

    def test_recovery_blocked_emits_verifier_reason_metadata(self) -> None:
        """Crash-recovery → verifier blocked_manual_review must surface the
        verifier's reason string (not None / a stale adapter error) in the
        observe + notify metadata.
        """
        from claw_v2.notebooklm_research_effect import build_research_effect_spec

        with tempfile.TemporaryDirectory() as tmp:
            jobs, store = _setup(tmp)
            job = jobs.enqueue(
                kind="notebooklm.research",
                payload={"notebook_id": "nb-1", "query": "q", "mode": "deep"},
            )
            # Pre-seed an interrupted attempt with the SAME idempotency key the
            # runner will compute: apply_in_progress, attempt=1, no result. The
            # runner's record_external_effect (ON CONFLICT DO NOTHING) loads this
            # row → _drive routes to _recover (NOT _apply), so the verifier runs.
            spec = build_research_effect_spec(
                job_id=job.job_id,
                notebook_id="nb-1",
                query="q",
                mode="deep",
                pre_intent_source_count=0,
            )
            store.record_external_effect(
                task_id=spec.task_id,
                run_id=spec.run_id,
                phase=spec.phase,
                effect_kind=spec.effect_kind,
                target=spec.target,
                request=spec.request,
                content_hash=spec.content_hash,
                job_id=spec.job_id,
                status="apply_in_progress",
                attempt_count=1,
            )

            observe_payloads: list[dict] = []

            class FakeObserve:
                def emit(self, event_type: str, payload: object = None) -> None:
                    if event_type == "notebooklm_research_effect_blocked_manual_review":
                        assert isinstance(payload, dict)
                        observe_payloads.append(payload)

            # status_fn returns no source_count → verifier classifies
            # blocked_manual_review with reason "source_count_missing".
            # The adapter (deep_research_fn) must NOT run on the recovery path.
            adapter_calls = {"n": 0}

            def _never_run(nb: str, q: str) -> int:
                adapter_calls["n"] += 1
                return 0

            notifier = MagicMock()
            runner = NotebookLMResearchRunner(
                job_service=jobs,
                store=store,
                observe=FakeObserve(),
                notifier=notifier,
                deep_research_fn=_never_run,
                status_fn=lambda nb: {},  # no source_count → blocked
            )
            ran = runner.run_once()
            self.assertTrue(ran)

            record = jobs.get(job.job_id)
            self.assertEqual(record.status, "failed")
            self.assertEqual(record.error, "effect_blocked_manual_review")
            self.assertEqual(adapter_calls["n"], 0)  # recovery path, no re-apply

            # The verifier's reason string (NOT None / a stale adapter error)
            # must surface in the observe + notify metadata.
            self.assertEqual(len(observe_payloads), 1)
            self.assertEqual(observe_payloads[0]["verifier_reason"], "source_count_missing")
            notifier.assert_called_once()


class RunnerDedupGuaranteeTests(unittest.TestCase):
    """The 'never duplicate' guarantee (spec §3, §5): on recovery the verifier
    must compare the ORIGINAL baseline (captured at first intent, persisted in
    the record) against the current count — NOT a baseline recomputed from the
    live count on the retry (which would always look unchanged → false
    verified_absent → re-run → duplicate sources).
    """

    def _preseed_interrupted(self, store, job_id: str, original_pre: int) -> None:
        """Pre-seed an interrupted apply_in_progress record whose persisted
        request.pre_intent_source_count is the ORIGINAL baseline."""
        from claw_v2.notebooklm_research_effect import build_research_effect_spec

        spec = build_research_effect_spec(
            job_id=job_id,
            notebook_id="nb-1",
            query="q",
            mode="deep",
            pre_intent_source_count=original_pre,
        )
        store.record_external_effect(
            task_id=spec.task_id,
            run_id=spec.run_id,
            phase=spec.phase,
            effect_kind=spec.effect_kind,
            target=spec.target,
            request=spec.request,
            content_hash=spec.content_hash,
            job_id=spec.job_id,
            status="apply_in_progress",
            attempt_count=1,
        )

    def test_count_moved_from_original_baseline_blocks_no_reapply(self) -> None:
        """Original baseline 0, current count 5 (sources WERE imported before the
        crash) → blocked_manual_review, adapter NOT re-run, job fail(retry=False).
        The anti-duplicate proof."""
        with tempfile.TemporaryDirectory() as tmp:
            jobs, store = _setup(tmp)
            job = jobs.enqueue(
                kind="notebooklm.research",
                payload={"notebook_id": "nb-1", "query": "q", "mode": "deep"},
            )
            self._preseed_interrupted(store, job.job_id, original_pre=0)

            adapter_calls = {"n": 0}

            def _never_run(nb: str, q: str) -> int:
                adapter_calls["n"] += 1
                return 9

            runner = NotebookLMResearchRunner(
                job_service=jobs,
                store=store,
                observe=None,
                notifier=None,
                deep_research_fn=_never_run,
                status_fn=lambda nb: {"source_count": 5},  # MOVED from original 0
            )
            ran = runner.run_once()
            self.assertTrue(ran)

            self.assertEqual(adapter_calls["n"], 0)  # NO re-apply → no duplicate
            record = jobs.get(job.job_id)
            self.assertEqual(record.status, "failed")
            self.assertEqual(record.error, "effect_blocked_manual_review")

            effects = store.list_external_effects()
            self.assertEqual(len(effects), 1)
            self.assertEqual(effects[0].status, "blocked_manual_review")

    def test_count_unchanged_from_original_baseline_safely_reapplies(self) -> None:
        """Original baseline 5, current count 5 (unchanged → clean no-op, nothing
        was imported) → verified_absent → the executor's controlled retry
        (verified_absent → _apply on the SAME key, spec §7) re-runs the adapter
        once and applies. Safe: count unchanged proves no prior import, so the
        single re-apply cannot duplicate."""
        with tempfile.TemporaryDirectory() as tmp:
            jobs, store = _setup(tmp)
            job = jobs.enqueue(
                kind="notebooklm.research",
                payload={"notebook_id": "nb-1", "query": "q", "mode": "deep"},
                max_attempts=3,
            )
            self._preseed_interrupted(store, job.job_id, original_pre=5)

            adapter_calls = {"n": 0}

            def _research(nb: str, q: str) -> int:
                adapter_calls["n"] += 1
                return 2

            runner = NotebookLMResearchRunner(
                job_service=jobs,
                store=store,
                observe=None,
                notifier=None,
                deep_research_fn=_research,
                status_fn=lambda nb: {"source_count": 5},  # UNCHANGED from original 5
            )
            ran = runner.run_once()
            self.assertTrue(ran)

            # verified_absent → controlled retry → adapter runs exactly once on
            # the SAME effect row (no duplicate row).
            self.assertEqual(adapter_calls["n"], 1)
            record = jobs.get(job.job_id)
            self.assertEqual(record.status, "completed")

            effects = store.list_external_effects()
            self.assertEqual(len(effects), 1)
            self.assertEqual(effects[0].status, "applied")


class RunnerAdapterExceptionTests(unittest.TestCase):
    def test_adapter_exception_fails_job_with_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jobs, store = _setup(tmp)
            job = jobs.enqueue(
                kind="notebooklm.research",
                payload={"notebook_id": "nb-1", "query": "q", "mode": "deep"},
                max_attempts=3,
            )

            def bad_research(nb: str, q: str) -> int:
                raise RuntimeError("CDP down")

            runner = NotebookLMResearchRunner(
                job_service=jobs,
                store=store,
                observe=None,
                notifier=None,
                deep_research_fn=bad_research,
                status_fn=lambda nb: {"source_count": 0},
            )
            ran = runner.run_once()
            self.assertTrue(ran)

            record = jobs.get(job.job_id)
            # adapter raise → retry=True → retrying (attempts < max)
            self.assertIn(record.status, ("retrying", "failed"))
            self.assertIn("CDP down", record.error)


# ---------------------------------------------------------------------------
# Task 3.4: start_research routing
# ---------------------------------------------------------------------------
class StartResearchRoutingTests(unittest.TestCase):
    def test_durable_on_enqueues_job_no_thread(self) -> None:
        """With research_durable=True, start_research enqueues a job and returns without
        spawning a thread."""
        with tempfile.TemporaryDirectory() as tmp:
            from claw_v2.notebooklm import NotebookLMService

            db_path = Path(tmp) / "claw.db"
            jobs = JobService(db_path)
            svc = NotebookLMService(job_service=jobs, research_durable=True)
            # CDP mode: no client_factory, no external backend
            result = svc.start_research("nb-full-id", "climate change", mode="deep")

            self.assertIn("encolado", result.lower())
            # No thread spawned
            self.assertEqual(len(svc._running), 0)
            # Job enqueued
            all_jobs = jobs.list()
            self.assertEqual(len(all_jobs), 1)
            self.assertEqual(all_jobs[0].kind, "notebooklm.research")
            self.assertEqual(all_jobs[0].payload["notebook_id"], "nb-full-id")
            self.assertEqual(all_jobs[0].payload["query"], "climate change")

    def test_durable_off_uses_thread_path(self) -> None:
        """With research_durable=False (default), start_research spawns a thread."""
        import time

        with tempfile.TemporaryDirectory() as tmp:
            from claw_v2.notebooklm import NotebookLMService

            db_path = Path(tmp) / "claw.db"
            jobs = JobService(db_path)
            svc = NotebookLMService(job_service=jobs)  # research_durable defaults False
            svc._cdp_research_fn = lambda nb, q: 2

            result = svc.start_research("nb-full-id", "query", mode="deep")
            self.assertIn("iniciado", result.lower())

            # Thread gets spawned; drain it
            deadline = time.time() + 2.0
            while time.time() < deadline and svc._running:
                time.sleep(0.01)

            # Job exists and completed
            all_jobs = jobs.list()
            self.assertEqual(len(all_jobs), 1)
            self.assertEqual(all_jobs[0].status, "completed")
