"""Phase 4 crash-resume synthetics for the NotebookLM durable lane.

One test per §8 crash window.  Each test:
  - drives everything through ``F2ExternalEffectExecutor.execute(...)`` with the
    real notebooklm adapter + verifier (fakes for ``deep_research_fn`` /
    ``status_fn``);
  - simulates the crash by pre-seeding the ``external_effect_records`` row via
    ``F2DurabilityStore`` *before* calling ``execute``;
  - asserts BOTH the resumed behaviour AND that there is exactly ONE effect row
    with no duplicate adapter apply (adapter call count).

See: docs/superpowers/plans/2026-06-24-f2-notebooklm-durable-lane.md §Phase 4
     docs/superpowers/specs/f2-notebooklm-research-durable-lane.md §8
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.external_effect_executor import F2ExternalEffectExecutor
from claw_v2.f2_durability_store import F2DurabilityStore
from claw_v2.notebooklm_research_effect import (
    build_research_effect_spec,
    notebooklm_research_adapter,
    notebooklm_research_verifier,
)
from claw_v2.sqlite_runtime import RuntimeDb


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _store(tmp: str) -> tuple[RuntimeDb, F2DurabilityStore]:
    db = RuntimeDb(Path(tmp) / "claw.db")
    return db, F2DurabilityStore(db)


def _spec(pre_count: int = 0):
    """Build a canonical research EffectSpec for synthetic tests."""
    return build_research_effect_spec(
        job_id="job:synth-1",
        notebook_id="nb-synth",
        query="crash resume test",
        mode="deep",
        pre_intent_source_count=pre_count,
    )


def _seed_effect(
    store: F2DurabilityStore,
    spec,
    *,
    status: str,
    attempt_count: int = 1,
    result: dict | None = None,
) -> None:
    """Pre-seed an external_effect_records row that simulates a mid-flight crash.

    Uses ``record_external_effect`` (ON CONFLICT DO NOTHING) with the given
    status / attempt_count / result so the executor's subsequent
    ``record_external_effect`` call will hit the conflict and load this row.
    """
    store.record_external_effect(
        task_id=spec.task_id,
        run_id=spec.run_id,
        phase=spec.phase,
        effect_kind=spec.effect_kind,
        target=spec.target,
        request=spec.request,
        content_hash=spec.content_hash,
        job_id=spec.job_id,
        verifier_kind=spec.verifier_kind,
        status=status,
        attempt_count=attempt_count,
        result=result,
    )


def _effect_count(store: F2DurabilityStore) -> int:
    return len(store.list_external_effects())


# ---------------------------------------------------------------------------
# 4.1 — crash before intent commit
# ---------------------------------------------------------------------------


class Window41CrashBeforeIntentTests(unittest.TestCase):
    """§8: no durable effect row exists; a fresh execute records intent and
    applies exactly once.  The adapter must be called once; one applied row."""

    def test_fresh_execute_records_intent_and_applies_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db, store = _store(tmp)
            self.addCleanup(db.close)

            calls: list[str] = []

            def deep_research(nb: str, q: str) -> int:
                calls.append(nb)
                return 4  # 4 sources imported → applied

            adapter = notebooklm_research_adapter(deep_research)
            verifier = notebooklm_research_verifier(status_fn=lambda nb: {"source_count": 4})
            executor = F2ExternalEffectExecutor(store)
            spec = _spec(pre_count=0)

            # No pre-seeded row — simulates crash BEFORE intent was committed.
            outcome = executor.execute(spec, adapter, verifier)

            effects = store.list_external_effects()
            self.assertEqual(outcome.status, "applied", "should be applied")
            self.assertEqual(len(effects), 1, "exactly one effect row")
            self.assertEqual(effects[0].status, "applied")
            self.assertEqual(effects[0].attempt_count, 1)
            self.assertEqual(len(calls), 1, "adapter called exactly once")


# ---------------------------------------------------------------------------
# 4.2 — crash after intent, before adapter (two reachable sub-paths)
#
# The executor short-circuits determinate states and only calls the verifier
# for genuinely ambiguous ones, so §8's single "after intent, before adapter"
# window splits into two distinct resume paths:
#   (a) crash AFTER the intent commit but BEFORE _apply transitions:
#       intent_recorded, attempt_count=0, no result → routed DIRECTLY to
#       _apply (NO verifier) → clean single apply.   [test 4.2a]
#   (b) crash INSIDE _apply (after the apply_in_progress transition, at/before
#       the adapter call): apply_in_progress, attempt_count=1, no result →
#       _recover → verifier(count unchanged) → verified_absent → re-apply. [4.2b]
# ---------------------------------------------------------------------------


class Window42aCrashAfterIntentBeforeApplyTests(unittest.TestCase):
    """§8-4.2 sub-path (a): crash AFTER the intent commit but BEFORE _apply
    transitioned the row.  State is intent_recorded, attempt_count=0, no result.
    On re-execute the executor routes intent_recorded/attempt-0 DIRECTLY to
    _apply (NO verifier) and applies once."""

    def test_fresh_intent_attempt0_applies_once_without_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db, store = _store(tmp)
            self.addCleanup(db.close)

            # Pre-seed: intent_recorded, attempt_count=0, NO result — the row was
            # committed by the intent step but the crash hit before _apply ran.
            spec = _spec(pre_count=0)
            _seed_effect(store, spec, status="intent_recorded", attempt_count=0)

            calls: list[str] = []

            def deep_research(nb: str, q: str) -> int:
                calls.append(nb)
                return 5  # sources imported

            adapter = notebooklm_research_adapter(deep_research)

            def verifier(_spec, _record):
                raise AssertionError(
                    "verifier must NOT run for intent_recorded/attempt-0 "
                    "(determinate → routed directly to _apply)"
                )

            executor = F2ExternalEffectExecutor(store)

            # execute() loads the seeded row (ON CONFLICT DO NOTHING), sees
            # intent_recorded with attempt_count==0 → _apply directly (no verifier).
            outcome = executor.execute(spec, adapter, verifier)

            effects = store.list_external_effects()
            self.assertEqual(outcome.status, "applied")
            self.assertEqual(len(effects), 1, "no duplicate effect row created")
            self.assertEqual(effects[0].status, "applied")
            self.assertEqual(effects[0].attempt_count, 1)
            self.assertEqual(len(calls), 1, "adapter called exactly once")


class Window42bCrashInsideApplyTests(unittest.TestCase):
    """§8-4.2 sub-path (b): crash INSIDE _apply — after the apply_in_progress
    transition, at/before the adapter call.  State is apply_in_progress
    (attempt 1, no result); status_fn shows count unchanged → _recover →
    verifier verified_absent → executor re-applies → applied.  Adapter called
    exactly once in this execute call (the recovery re-apply)."""

    def test_in_progress_count_unchanged_verified_absent_then_applied(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db, store = _store(tmp)
            self.addCleanup(db.close)

            # Pre-seed: intent recorded, apply_in_progress, attempt=1, NO result.
            spec = _spec(pre_count=0)  # pre_intent_source_count=0 in persisted request
            _seed_effect(store, spec, status="apply_in_progress", attempt_count=1)

            calls: list[str] = []

            def deep_research(nb: str, q: str) -> int:
                calls.append(nb)
                return 5  # sources imported on the retry

            # status_fn returns count=0 (same as pre_intent_source_count=0).
            adapter = notebooklm_research_adapter(deep_research)
            verifier = notebooklm_research_verifier(status_fn=lambda nb: {"source_count": 0})
            executor = F2ExternalEffectExecutor(store)

            # execute() loads the seeded row (ON CONFLICT DO NOTHING), sees
            # apply_in_progress with attempt>0 and no result → _recover →
            # verifier(count unchanged, no result) → verified_absent →
            # _apply → adapter called once.
            outcome = executor.execute(spec, adapter, verifier)

            effects = store.list_external_effects()
            self.assertEqual(outcome.status, "applied")
            self.assertEqual(len(effects), 1, "no duplicate effect row created")
            self.assertEqual(effects[0].status, "applied")
            self.assertEqual(len(calls), 1, "adapter called exactly once (re-apply only)")


# ---------------------------------------------------------------------------
# 4.3 — crash after adapter, before result commit
# ---------------------------------------------------------------------------


class Window43CrashAfterAdapterBeforeResultCommitTests(unittest.TestCase):
    """§8: state is apply_in_progress (attempt 1, no result); status_fn shows
    count MOVED (sources were imported but result never committed) → verifier
    blocked_manual_review.  Adapter must NOT be called; one blocked row."""

    def test_count_moved_no_result_is_blocked_manual_review_no_adapter_call(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db, store = _store(tmp)
            self.addCleanup(db.close)

            # Pre-seed: apply_in_progress, attempt=1, NO result, baseline=0.
            spec = _spec(pre_count=0)
            _seed_effect(store, spec, status="apply_in_progress", attempt_count=1)

            calls: list[str] = []

            def deep_research(nb: str, q: str) -> int:
                calls.append(nb)
                return 5  # should never be reached

            # status_fn returns count=5 → count MOVED from baseline 0 → blocked.
            adapter = notebooklm_research_adapter(deep_research)
            verifier = notebooklm_research_verifier(status_fn=lambda nb: {"source_count": 5})
            executor = F2ExternalEffectExecutor(store)

            outcome = executor.execute(spec, adapter, verifier)

            effects = store.list_external_effects()
            self.assertEqual(
                outcome.status,
                "blocked_manual_review",
                "ambiguous state must be blocked",
            )
            self.assertEqual(len(effects), 1, "no duplicate effect row")
            self.assertEqual(effects[0].status, "blocked_manual_review")
            self.assertEqual(len(calls), 0, "adapter must NOT be called (anti-duplicate guarantee)")


# ---------------------------------------------------------------------------
# 4.4 — crash after result commit, before job complete
# ---------------------------------------------------------------------------


class Window44CrashAfterResultBeforeJobCompleteTests(unittest.TestCase):
    """§8: state is 'applied' with a result (adapter finished and result was
    committed, but the job-complete step never ran).  execute() must return
    'applied' immediately without calling the adapter.  No duplicate row.

    NOTE: the executor short-circuits a committed 'applied' result — it returns
    immediately WITHOUT calling the verifier.  This is correct and intentionally
    tighter than §8's looser "→ verifier → verified_applied" wording: a
    committed result is determinate, so no verification is needed."""

    def test_applied_row_returns_applied_without_adapter_or_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db, store = _store(tmp)
            self.addCleanup(db.close)

            # Pre-seed: applied with result — simulates crash after result commit.
            spec = _spec(pre_count=0)
            _seed_effect(
                store,
                spec,
                status="applied",
                attempt_count=1,
                result={"imported_count": 3},
            )

            calls: list[str] = []

            def deep_research(nb: str, q: str) -> int:
                calls.append(nb)
                return 3  # should never be reached

            verifier_calls: list[str] = []

            def verifier(_spec, record):
                verifier_calls.append(record.external_effect_id)
                raise AssertionError(
                    "verifier must NOT run on an idempotent committed-'applied' "
                    "re-entry (short-circuited as determinate)"
                )

            adapter = notebooklm_research_adapter(deep_research)
            executor = F2ExternalEffectExecutor(store)

            outcome = executor.execute(spec, adapter, verifier)

            effects = store.list_external_effects()
            self.assertEqual(outcome.status, "applied", "idempotent re-entry → applied")
            self.assertEqual(len(effects), 1, "no duplicate effect row")
            self.assertEqual(effects[0].status, "applied")
            self.assertEqual(len(calls), 0, "adapter must NOT be called (idempotent re-entry)")
            self.assertEqual(
                len(verifier_calls),
                0,
                "verifier must NOT be called (committed result short-circuits)",
            )


if __name__ == "__main__":
    unittest.main()
