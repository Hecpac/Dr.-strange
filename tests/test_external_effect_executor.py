from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.f2_durability_store import F2DurabilityStore
from claw_v2.sqlite_runtime import RuntimeDb
from claw_v2.external_effect_executor import (
    AdapterResult,
    EffectSpec,
    F2ExternalEffectExecutor,
    VerifierVerdict,
)


def _store(tmp: str) -> tuple[F2DurabilityStore, RuntimeDb]:
    db = RuntimeDb(Path(tmp) / "claw.db")
    return F2DurabilityStore(db), db


def _spec(**kw) -> EffectSpec:  # type: ignore[type-arg]
    base = dict(
        task_id="t1",
        run_id="r1",
        phase="research",
        effect_kind="demo_effect",
        target="nb1",
        request={"q": "x"},
        content_hash="ch1",
        job_id="job:1",
        verifier_kind="demo",
    )
    base.update(kw)
    return EffectSpec(**base)


class ExecutorHappyPathTests(unittest.TestCase):
    def test_happy_path_records_intent_then_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = _store(tmp)
            ex = F2ExternalEffectExecutor(store)
            calls: list[str] = []

            def adapter(spec: EffectSpec) -> AdapterResult:
                calls.append(spec.run_id)
                return AdapterResult(applied=True, result={"imported_count": 3})

            def verifier(spec: EffectSpec, record: object) -> VerifierVerdict:
                raise AssertionError("verifier must not run on happy path")

            outcome = ex.execute(_spec(), adapter, verifier)
            self.assertEqual(outcome.status, "applied")
            self.assertEqual(calls, ["r1"])
            self.assertEqual(outcome.record.status, "applied")
            self.assertEqual(outcome.record.attempt_count, 1)
            self.assertFalse(outcome.should_retry)


class ExecutorDedupTests(unittest.TestCase):
    def test_reentry_on_applied_does_not_recall_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = _store(tmp)
            ex = F2ExternalEffectExecutor(store)
            n = {"calls": 0}

            def adapter(spec: EffectSpec) -> AdapterResult:
                n["calls"] += 1
                return AdapterResult(applied=True, result={"imported_count": 2})

            def verifier(spec: EffectSpec, record: object) -> VerifierVerdict:
                raise AssertionError("no verifier")

            spec = _spec()
            ex.execute(spec, adapter, verifier)  # first run -> applied
            outcome = ex.execute(spec, adapter, verifier)  # same key -> dedup
            self.assertEqual(n["calls"], 1)
            self.assertEqual(outcome.status, "applied")
            self.assertEqual(outcome.record.status, "applied")


class ExecutorRecoveryTests(unittest.TestCase):
    def test_interrupted_intent_runs_verifier_verified_absent_then_retries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = _store(tmp)
            ex = F2ExternalEffectExecutor(store)
            spec = _spec()
            # simulate prior interrupted attempt: intent recorded, apply_in_progress,
            # attempt=1, no result
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
            n = {"calls": 0}

            def adapter(spec: EffectSpec) -> AdapterResult:
                n["calls"] += 1
                return AdapterResult(applied=True, result={"imported_count": 4})

            def verifier(spec: EffectSpec, record: object) -> VerifierVerdict:
                return VerifierVerdict("verified_absent", {"reason": "no-op"}, "count_unchanged")

            outcome = ex.execute(spec, adapter, verifier)
            self.assertEqual(outcome.status, "applied")  # verified_absent -> retry -> applied
            self.assertEqual(n["calls"], 1)
            self.assertEqual(outcome.record.attempt_count, 2)

    def test_recovery_verified_applied_completes_without_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = _store(tmp)
            ex = F2ExternalEffectExecutor(store)
            spec = _spec()
            store.record_external_effect(
                task_id=spec.task_id,
                run_id=spec.run_id,
                phase=spec.phase,
                effect_kind=spec.effect_kind,
                target=spec.target,
                request=spec.request,
                content_hash=spec.content_hash,
                status="apply_in_progress",
                attempt_count=1,
            )

            def adapter(spec: EffectSpec) -> AdapterResult:
                raise AssertionError("adapter must not run")

            def verifier(spec: EffectSpec, record: object) -> VerifierVerdict:
                return VerifierVerdict("verified_applied", {"imported_count": 5}, "result_present")

            outcome = ex.execute(spec, adapter, verifier)
            self.assertEqual(outcome.status, "verified_applied")

    def test_recovery_blocked_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = _store(tmp)
            ex = F2ExternalEffectExecutor(store)
            spec = _spec()
            store.record_external_effect(
                task_id=spec.task_id,
                run_id=spec.run_id,
                phase=spec.phase,
                effect_kind=spec.effect_kind,
                target=spec.target,
                request=spec.request,
                content_hash=spec.content_hash,
                status="apply_in_progress",
                attempt_count=1,
            )

            def adapter(spec: EffectSpec) -> AdapterResult:
                raise AssertionError("adapter must not run")

            def verifier(spec: EffectSpec, record: object) -> VerifierVerdict:
                return VerifierVerdict("blocked_manual_review", {}, "ambiguous")

            outcome = ex.execute(spec, adapter, verifier)
            self.assertEqual(outcome.status, "blocked_manual_review")

    def test_adapter_raise_leaves_apply_in_progress_then_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = _store(tmp)
            ex = F2ExternalEffectExecutor(store)
            spec = _spec()
            n = {"calls": 0}

            def raising_adapter(spec: EffectSpec) -> AdapterResult:
                n["calls"] += 1
                raise RuntimeError("boom")

            def no_verifier(spec: EffectSpec, record: object) -> VerifierVerdict:
                raise AssertionError("verifier must not run on first attempt")

            # First attempt: adapter raises -> execute re-raises.
            with self.assertRaises(RuntimeError):
                ex.execute(spec, raising_adapter, no_verifier)
            self.assertEqual(n["calls"], 1)
            key = store.list_external_effects()[0].idempotency_key
            interrupted = store.get_external_effect_by_idempotency_key(key)
            assert interrupted is not None
            self.assertEqual(interrupted.status, "apply_in_progress")
            self.assertEqual(interrupted.attempt_count, 1)
            self.assertIsNotNone(interrupted.error)

            # Second attempt: verifier says absent -> retry; adapter now succeeds.
            def ok_adapter(spec: EffectSpec) -> AdapterResult:
                n["calls"] += 1
                return AdapterResult(applied=True, result={"imported_count": 7})

            def absent_verifier(spec: EffectSpec, record: object) -> VerifierVerdict:
                return VerifierVerdict("verified_absent", {"reason": "no-op"}, "count_unchanged")

            outcome = ex.execute(spec, ok_adapter, absent_verifier)
            self.assertEqual(n["calls"], 2)
            self.assertEqual(outcome.status, "applied")
            self.assertEqual(outcome.record.attempt_count, 2)


class ExecutorAttemptBudgetTests(unittest.TestCase):
    def test_apply_past_attempt_budget_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = _store(tmp)
            ex = F2ExternalEffectExecutor(store)
            spec = _spec(max_attempts=2)
            store.record_external_effect(
                task_id=spec.task_id,
                run_id=spec.run_id,
                phase=spec.phase,
                effect_kind=spec.effect_kind,
                target=spec.target,
                request=spec.request,
                content_hash=spec.content_hash,
                status="verified_absent",
                attempt_count=2,
            )

            def adapter(spec: EffectSpec) -> AdapterResult:
                raise AssertionError("must not run past budget")

            def verifier(spec: EffectSpec, record: object) -> VerifierVerdict:
                raise AssertionError("no verifier")

            outcome = ex.execute(spec, adapter, verifier)
            self.assertEqual(outcome.status, "blocked_manual_review")
