"""PR 0B: meta/introspection routing guard.

Unit tests for the classifier in `claw_v2.bot_helpers` and the defensive
coordinator hard-guard in `claw_v2.task_handler.TaskHandler`. The
classifier must:

  - flag reflective / clarification / audit / secret-shaped inputs,
  - leave explicit implementation requests alone, and
  - resolve mixed prompts in favor of implementation.

The coordinator hard guard must refuse to start coding work for inputs
the classifier marks as meta, while emitting
`coordinator_rejected_non_actionable_objective` so future audits can
trace the rejection.
"""

from __future__ import annotations

import unittest

from claw_v2.bot_helpers import (
    MetaIntrospectionIntent,
    detect_meta_introspection_request,
    has_explicit_implementation_request,
)
from claw_v2.task_handler import TaskHandler


class _RecordingObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **kwargs: object) -> None:
        self.events.append((event, dict(kwargs)))


class _StubCoordinator:
    """Sentinel — presence indicates a coordinator is wired."""


def _make_task_handler_with_observe() -> tuple[TaskHandler, _RecordingObserve]:
    observe = _RecordingObserve()
    # TaskHandler only inspects `self.coordinator` and `self.observe` on the
    # guard path; everything else is unreachable in these tests.
    handler = TaskHandler.__new__(TaskHandler)
    handler.coordinator = _StubCoordinator()
    handler.observe = observe
    return handler, observe


class MetaIntrospectionClassifierTests(unittest.TestCase):
    """Test set A/B/C/D/E/F from PR 0B spec."""

    # --- A. Pure meta prompts ---------------------------------------------

    def test_a_meta_prompts_are_classified_as_meta(self) -> None:
        meta_phrases = [
            "¿por qué no completas tareas fáciles?",
            "¿por qué frenas en tareas reversibles?",
            "¿entendiste?",
            "dime si esta lectura es correcta",
            "analiza esta conversación",
            "qué opinas de esta respuesta del bot",
            "why did you fail?",
            "what went wrong?",
            "analyze this behavior",
            "do you understand?",
        ]
        for phrase in meta_phrases:
            with self.subTest(phrase=phrase):
                intent = detect_meta_introspection_request(phrase)
                self.assertIsNotNone(intent, f"expected meta match for: {phrase!r}")
                assert intent is not None  # for type narrowing
                self.assertEqual(intent.kind, "meta")
                self.assertTrue(intent.reason.startswith("meta_pattern:"))

    # --- B. Clarification question ----------------------------------------

    def test_b_clarification_question_is_meta_not_coordinator_bait(self) -> None:
        intent = detect_meta_introspection_request("Qué queremos comunicar en el email?")
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.kind, "meta")

    # --- C. Secret-shaped token -------------------------------------------

    def test_c_secret_shaped_token_is_non_actionable(self) -> None:
        # Synthetic token mirroring the shape we found in the audit DB
        # (mixed-case alphanumeric, 20 chars, no whitespace). NOT a real
        # secret; constructed for this test only.
        fake_token = "9aBcDeFgHi1234567jKl"
        intent = detect_meta_introspection_request(fake_token)
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.kind, "non_actionable_token")
        self.assertTrue(intent.normalized_text.startswith("<redacted:"))
        self.assertNotIn(fake_token, intent.normalized_text)
        self.assertNotIn(fake_token, intent.reason)

    def test_c2_normal_short_tokens_are_not_flagged(self) -> None:
        # Task IDs and short words must not trip the heuristic.
        for benign in ("tg-574707975", "perfecto", "ok", "implementa", "claude"):
            with self.subTest(benign=benign):
                intent = detect_meta_introspection_request(benign)
                self.assertNotEqual(
                    getattr(intent, "kind", None),
                    "non_actionable_token",
                    f"benign input {benign!r} false-positived as token",
                )

    # --- D. Audit request -------------------------------------------------

    def test_d_log_audit_request_is_audit_not_coding(self) -> None:
        intent = detect_meta_introspection_request("investiga los logs de esta tarea fallida")
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.kind, "audit")
        self.assertTrue(intent.reason.startswith("audit_pattern:"))

    # --- E. Explicit implementation still allowed -------------------------

    def test_e_explicit_implementation_requests_are_not_blocked(self) -> None:
        cases = [
            "implementa el fix",
            "parchea bot.py",
            "agrega tests para owner delegation",
            "fix the bug",
            "implement the fix",
            "patch bot.py",
            "add tests",
            "apply the change",
            "write the patch",
            "modifica el modulo de routing",
            "corrige el bug ahora",
            "aplica el fix",
        ]
        for phrase in cases:
            with self.subTest(phrase=phrase):
                self.assertTrue(
                    has_explicit_implementation_request(phrase),
                    f"expected implementation classifier to match: {phrase!r}",
                )
                self.assertIsNone(
                    detect_meta_introspection_request(phrase),
                    f"implementation request should NOT register as meta: {phrase!r}",
                )

    def test_e2_past_tense_implementation_question_is_still_meta(self) -> None:
        # "por qué no implementaste el fix" — past tense ≠ imperative.
        # Meta wins because there is no current-tense implement verb.
        intent = detect_meta_introspection_request("por que no completaste el fix")
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.kind, "meta")

    # --- F. Mixed prompt — implementation wins ----------------------------

    def test_f_mixed_prompt_prefers_implementation(self) -> None:
        mixed = "analiza este bug y luego implementa el fix"
        self.assertTrue(has_explicit_implementation_request(mixed))
        self.assertIsNone(detect_meta_introspection_request(mixed))

    # --- Accent insensitivity ---------------------------------------------

    def test_accent_insensitive_matching(self) -> None:
        with_accents = "¿Por qué fallaste en esa tarea?"
        without_accents = "por que fallaste en esa tarea?"
        self.assertEqual(
            detect_meta_introspection_request(with_accents).kind,  # type: ignore[union-attr]
            detect_meta_introspection_request(without_accents).kind,  # type: ignore[union-attr]
        )

    # --- Negative: ordinary chat is not meta ------------------------------

    def test_ordinary_chat_messages_are_not_meta(self) -> None:
        for benign in (
            "hola",
            "gracias",
            "envia el informe diario a las 9am",
            "abre la página de Linear",
            "create a Linear issue for the deploy",
        ):
            with self.subTest(benign=benign):
                self.assertIsNone(detect_meta_introspection_request(benign))


class CoordinatorHardGuardTests(unittest.TestCase):
    """Defensive guard inside TaskHandler must refuse meta objectives."""

    def test_maybe_run_coordinated_task_rejects_meta_question(self) -> None:
        handler, observe = _make_task_handler_with_observe()
        result = handler.maybe_run_coordinated_task(
            "tg-test", "Mi pregunta es porque no completas las tareas faciles?"
        )
        self.assertIsNone(result)
        events = [name for name, _ in observe.events]
        self.assertIn("coordinator_rejected_non_actionable_objective", events)

    def test_maybe_run_coordinated_task_rejects_clarification(self) -> None:
        handler, observe = _make_task_handler_with_observe()
        result = handler.maybe_run_coordinated_task(
            "tg-test", "Que queremos comunicar en el email?"
        )
        self.assertIsNone(result)
        payloads = [
            kwargs.get("payload") for name, kwargs in observe.events
            if name == "coordinator_rejected_non_actionable_objective"
        ]
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["source"], "maybe_run_coordinated_task")
        self.assertEqual(payloads[0]["kind"], "meta")

    def test_maybe_run_coordinated_task_rejects_secret_shaped_token(self) -> None:
        handler, observe = _make_task_handler_with_observe()
        fake_token = "9aBcDeFgHi1234567jKl"
        result = handler.maybe_run_coordinated_task("tg-test", fake_token)
        self.assertIsNone(result)
        rejections = [
            kwargs.get("payload") for name, kwargs in observe.events
            if name == "coordinator_rejected_non_actionable_objective"
        ]
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["kind"], "non_actionable_token")
        # Redaction: raw token must NOT appear in the observability payload.
        for value in rejections[0].values():
            self.assertNotIn(fake_token, str(value))

    def test_start_autonomous_task_rejects_meta_with_safe_message(self) -> None:
        handler, observe = _make_task_handler_with_observe()
        response = handler.start_autonomous_task(
            "tg-test", "analiza esta conversacion"
        )
        self.assertIn("pregunta reflexiva", response)
        self.assertIn("implementa el fix", response)
        events = [name for name, _ in observe.events]
        self.assertIn("coordinator_rejected_non_actionable_objective", events)

    def test_maybe_run_coordinated_task_does_not_reject_implementation_request(self) -> None:
        handler, observe = _make_task_handler_with_observe()
        # The implementation request must NOT be rejected by the meta guard.
        # We can't run the full coordinator here (no state, no LLM router),
        # so the call will fail downstream — but it must NOT emit the
        # rejection event, and the failure must come from a later stage.
        try:
            handler.maybe_run_coordinated_task("tg-test", "implementa el fix")
        except Exception:
            pass  # expected — handler is partially initialized for this test
        events = [name for name, _ in observe.events]
        self.assertNotIn("coordinator_rejected_non_actionable_objective", events)

    def test_audit_request_is_rejected_by_coordinator_but_classified_as_audit(self) -> None:
        handler, observe = _make_task_handler_with_observe()
        result = handler.maybe_run_coordinated_task(
            "tg-test", "investiga los logs de esta tarea fallida"
        )
        self.assertIsNone(result)
        rejections = [
            kwargs.get("payload") for name, kwargs in observe.events
            if name == "coordinator_rejected_non_actionable_objective"
        ]
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["kind"], "audit")


class MetaIntrospectionIntentShapeTests(unittest.TestCase):
    def test_intent_dataclass_is_frozen(self) -> None:
        intent = MetaIntrospectionIntent(kind="meta", normalized_text="x", reason="y")
        with self.assertRaises(Exception):
            # frozen dataclass: assignment must fail
            intent.kind = "audit"  # type: ignore[misc]

    def test_empty_input_returns_none(self) -> None:
        self.assertIsNone(detect_meta_introspection_request(""))
        self.assertIsNone(detect_meta_introspection_request("   "))


if __name__ == "__main__":
    unittest.main()
