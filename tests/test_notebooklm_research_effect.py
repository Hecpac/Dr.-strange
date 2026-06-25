"""Tests for notebooklm_research_effect — Phase 2 (Tasks 2.1–2.3).

TDD: tests are written first; the module is created after the initial red run.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from claw_v2.f2_durability_store import F2DurabilityStore
from claw_v2.sqlite_runtime import RuntimeDb


def _store(tmp: str) -> F2DurabilityStore:
    db = RuntimeDb(Path(tmp) / "claw.db")
    return F2DurabilityStore(db)


def _nb_spec(pre_count: int = 0):
    from claw_v2.notebooklm_research_effect import build_research_effect_spec

    return build_research_effect_spec(
        job_id="job:1",
        notebook_id="nb-1",
        query="q",
        mode="deep",
        pre_intent_source_count=pre_count,
    )


def _record_with_result(tmp: str, imported_count: int = 2):
    """Return an ExternalEffectRecord that has a result_json set."""
    store = _store(tmp)
    spec = _nb_spec()
    rec = store.record_external_effect(
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
        result={"imported_count": imported_count},
    )
    return rec


def _record_no_result(tmp: str):
    """Return an ExternalEffectRecord with no result_json (interrupted apply)."""
    store = _store(tmp)
    spec = _nb_spec()
    rec = store.record_external_effect(
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
    return rec


# ---------------------------------------------------------------------------
# Task 2.1 — build_research_effect_spec
# ---------------------------------------------------------------------------


class BuildSpecTests(unittest.TestCase):
    def test_spec_identity_and_request(self):
        from claw_v2.notebooklm_research_effect import build_research_effect_spec

        spec = build_research_effect_spec(
            job_id="job:abc",
            notebook_id="nb-1",
            query="climate",
            mode="deep",
            pre_intent_source_count=2,
        )
        self.assertEqual(spec.effect_kind, "notebooklm_research")
        self.assertEqual(spec.target, "nb-1")
        self.assertEqual(spec.run_id, "job:abc")
        self.assertEqual(spec.phase, "research")
        self.assertEqual(spec.verifier_kind, "notebooklm_research")
        self.assertEqual(spec.request["pre_intent_source_count"], 2)
        expected = hashlib.sha256("climate|deep".encode()).hexdigest()
        self.assertEqual(spec.content_hash, expected)

    def test_task_id_defaults_to_job_id(self):
        from claw_v2.notebooklm_research_effect import build_research_effect_spec

        spec = build_research_effect_spec(
            job_id="job:xyz",
            notebook_id="nb-2",
            query="ai",
            mode="quick",
            pre_intent_source_count=0,
        )
        self.assertEqual(spec.task_id, "job:xyz")

    def test_task_id_explicit_override(self):
        from claw_v2.notebooklm_research_effect import build_research_effect_spec

        spec = build_research_effect_spec(
            job_id="job:xyz",
            notebook_id="nb-2",
            query="ai",
            mode="quick",
            pre_intent_source_count=0,
            task_id="task:custom",
        )
        self.assertEqual(spec.task_id, "task:custom")

    def test_request_contains_expected_keys(self):
        from claw_v2.notebooklm_research_effect import build_research_effect_spec

        spec = build_research_effect_spec(
            job_id="job:1",
            notebook_id="nb-1",
            query="q",
            mode="deep",
            pre_intent_source_count=5,
        )
        self.assertIn("notebook_id", spec.request)
        self.assertIn("query", spec.request)
        self.assertIn("mode", spec.request)
        self.assertIn("pre_intent_source_count", spec.request)
        self.assertEqual(spec.request["notebook_id"], "nb-1")
        self.assertEqual(spec.request["query"], "q")
        self.assertEqual(spec.request["mode"], "deep")
        self.assertEqual(spec.request["pre_intent_source_count"], 5)


# ---------------------------------------------------------------------------
# Task 2.2 — notebooklm_research_adapter
# ---------------------------------------------------------------------------


class AdapterTests(unittest.TestCase):
    def test_positive_import_is_applied(self):
        from claw_v2.notebooklm_research_effect import notebooklm_research_adapter

        def deep_research(nb, q):
            return 3

        ar = notebooklm_research_adapter(deep_research)(_nb_spec())
        self.assertTrue(ar.applied)
        self.assertEqual(ar.result["imported_count"], 3)

    def test_zero_import_is_not_applied(self):
        from claw_v2.notebooklm_research_effect import notebooklm_research_adapter

        def deep_research(nb, q):
            return 0

        ar = notebooklm_research_adapter(deep_research)(_nb_spec())
        self.assertFalse(ar.applied)
        self.assertEqual(ar.result["imported_count"], 0)

    def test_none_return_treated_as_zero(self):
        from claw_v2.notebooklm_research_effect import notebooklm_research_adapter

        def deep_research(nb, q):
            return None

        ar = notebooklm_research_adapter(deep_research)(_nb_spec())
        self.assertFalse(ar.applied)
        self.assertEqual(ar.result["imported_count"], 0)

    def test_zero_import_reason_is_set(self):
        from claw_v2.notebooklm_research_effect import notebooklm_research_adapter

        def deep_research(nb, q):
            return 0

        ar = notebooklm_research_adapter(deep_research)(_nb_spec())
        self.assertIsNotNone(ar.reason)
        self.assertEqual(ar.reason, "zero_imports")

    def test_positive_import_reason_is_none(self):
        from claw_v2.notebooklm_research_effect import notebooklm_research_adapter

        def deep_research(nb, q):
            return 1

        ar = notebooklm_research_adapter(deep_research)(_nb_spec())
        self.assertIsNone(ar.reason)

    def test_adapter_passes_correct_args_to_fn(self):
        from claw_v2.notebooklm_research_effect import notebooklm_research_adapter

        calls = []

        def deep_research(nb, q):
            calls.append((nb, q))
            return 2

        spec = _nb_spec()
        notebooklm_research_adapter(deep_research)(spec)
        self.assertEqual(calls, [("nb-1", "q")])


# ---------------------------------------------------------------------------
# Task 2.3 — notebooklm_research_verifier
# ---------------------------------------------------------------------------


class VerifierTests(unittest.TestCase):
    def test_result_present_is_verified_applied(self):
        from claw_v2.notebooklm_research_effect import notebooklm_research_verifier

        with tempfile.TemporaryDirectory() as tmp:
            rec = _record_with_result(tmp, imported_count=2)
            v = notebooklm_research_verifier(status_fn=lambda nb: {"source_count": 5})
            verdict = v(_nb_spec(), rec)
            self.assertEqual(verdict.classification, "verified_applied")

    def test_count_unchanged_no_result_is_verified_absent(self):
        from claw_v2.notebooklm_research_effect import notebooklm_research_verifier

        with tempfile.TemporaryDirectory() as tmp:
            rec = _record_no_result(tmp)
            v = notebooklm_research_verifier(status_fn=lambda nb: {"source_count": 0})
            verdict = v(_nb_spec(pre_count=0), rec)
            self.assertEqual(verdict.classification, "verified_absent")

    def test_count_moved_no_result_is_blocked(self):
        from claw_v2.notebooklm_research_effect import notebooklm_research_verifier

        with tempfile.TemporaryDirectory() as tmp:
            rec = _record_no_result(tmp)
            v = notebooklm_research_verifier(status_fn=lambda nb: {"source_count": 3})
            verdict = v(_nb_spec(pre_count=0), rec)
            self.assertEqual(verdict.classification, "blocked_manual_review")

    def test_status_fn_raises_is_blocked(self):
        from claw_v2.notebooklm_research_effect import notebooklm_research_verifier

        def failing_status(nb):
            raise RuntimeError("network timeout")

        with tempfile.TemporaryDirectory() as tmp:
            rec = _record_no_result(tmp)
            v = notebooklm_research_verifier(status_fn=failing_status)
            verdict = v(_nb_spec(pre_count=0), rec)
            self.assertEqual(verdict.classification, "blocked_manual_review")
            self.assertIn("error", verdict.verification)

    def test_status_fn_returns_no_source_count_is_blocked(self):
        from claw_v2.notebooklm_research_effect import notebooklm_research_verifier

        with tempfile.TemporaryDirectory() as tmp:
            rec = _record_no_result(tmp)
            v = notebooklm_research_verifier(status_fn=lambda nb: {})
            verdict = v(_nb_spec(pre_count=0), rec)
            self.assertEqual(verdict.classification, "blocked_manual_review")

    def test_unknown_pre_count_no_result_is_blocked(self):
        """pre_intent_source_count absent in request → blocked_manual_review."""
        from claw_v2.notebooklm_research_effect import notebooklm_research_verifier
        from claw_v2.external_effect_executor import EffectSpec

        spec = EffectSpec(
            task_id="t1",
            run_id="r1",
            phase="research",
            effect_kind="notebooklm_research",
            target="nb-1",
            request={
                "notebook_id": "nb-1",
                "query": "q",
                "mode": "deep",
            },  # no pre_intent_source_count
            content_hash="ch1",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            rec = store.record_external_effect(
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
            v = notebooklm_research_verifier(status_fn=lambda nb: {"source_count": 0})
            verdict = v(spec, rec)
            self.assertEqual(verdict.classification, "blocked_manual_review")

    def test_verified_applied_reason_string(self):
        from claw_v2.notebooklm_research_effect import notebooklm_research_verifier

        with tempfile.TemporaryDirectory() as tmp:
            rec = _record_with_result(tmp, imported_count=3)
            v = notebooklm_research_verifier(status_fn=lambda nb: {"source_count": 3})
            verdict = v(_nb_spec(), rec)
            self.assertIsInstance(verdict.reason, str)
            self.assertGreater(len(verdict.reason), 0)

    def test_verified_absent_verification_contains_pre_current(self):
        from claw_v2.notebooklm_research_effect import notebooklm_research_verifier

        with tempfile.TemporaryDirectory() as tmp:
            rec = _record_no_result(tmp)
            v = notebooklm_research_verifier(status_fn=lambda nb: {"source_count": 7})
            # pre_count == current_count (both 7) → verified_absent
            spec = _nb_spec(pre_count=7)
            verdict = v(spec, rec)
            self.assertEqual(verdict.classification, "verified_absent")
            self.assertEqual(verdict.verification["pre"], 7)
            self.assertEqual(verdict.verification["current"], 7)
