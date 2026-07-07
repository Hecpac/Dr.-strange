from __future__ import annotations

import unittest

from claw_v2.learning import LearningLoop, _classify_transient_automation_failure

# A3.9: transient-vs-replayable taxonomy. Transient browser/computer failures
# are telemetry, never replayable lessons; genuinely replayable user/task
# lessons keep persisting. All fixtures are synthetic phrases.


class _FakeMemory:
    def __init__(self) -> None:
        self.stored: list[dict] = []

    def store_task_outcome_with_embedding(self, **kwargs) -> int:
        self.stored.append(kwargs)
        return len(self.stored)

    def update_calibration_stats(self, task_type: str) -> None:  # pragma: no cover
        pass


class _FakeObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, payload: dict | None = None, **_kwargs) -> None:
        self.events.append((event_type, dict(payload or {})))


def _loop() -> tuple[LearningLoop, _FakeMemory, _FakeObserve]:
    memory = _FakeMemory()
    observe = _FakeObserve()
    return LearningLoop(memory=memory, router=None, observe=observe), memory, observe


class TransientFailuresAreNotReplayableLessonsTests(unittest.TestCase):
    def test_generic_transients_on_automation_types_are_skipped(self) -> None:
        cases = [
            ("browse", "Navigation timeout after 30s waiting for page", "timeout"),
            ("computer", "Stopped at iteration limit before finishing", "iteration_limit"),
            ("browse", "(no result)", "no_result"),
            (
                "computer",
                "Computer task drifted outside requested scope: scope drift",
                "scope_drift",
            ),
            ("browse", "browser_use unavailable in this runtime", "browser_unavailable"),
        ]
        for task_type, snippet, expected_reason in cases:
            loop, memory, observe = _loop()
            oid = loop.record(
                task_type=task_type,
                task_id="t1",
                description=f"{task_type} task",
                approach="automation",
                outcome="failure",
                error_snippet=snippet,
            )
            self.assertIsNone(oid, snippet)
            self.assertEqual(memory.stored, [], snippet)
            skipped = [p for e, p in observe.events if e == "learning_transient_skipped"]
            self.assertEqual(len(skipped), 1, snippet)
            self.assertEqual(skipped[0]["reason"], expected_reason, snippet)

    def test_typed_transient_reason_code_gates_regardless_of_task_type(self) -> None:
        loop, memory, observe = _loop()
        oid = loop.record(
            task_type="cycle",
            task_id="t2",
            description="turn post-mortem",
            approach="cycle",
            outcome="failure",
            error_snippet="worker stopped",
            reason_code="scope_drift",
            retryable=True,
        )
        self.assertIsNone(oid)
        self.assertEqual(memory.stored, [])
        skipped = [p for e, p in observe.events if e == "learning_transient_skipped"]
        self.assertEqual(skipped[0]["reason"], "scope_drift")
        self.assertEqual(skipped[0]["reason_code"], "scope_drift")

    def test_telemetry_payload_is_enum_slugs_never_free_text(self) -> None:
        loop, _memory, observe = _loop()
        loop.record(
            task_type="browse",
            task_id="t3",
            description="Browse error for https://example.com",
            approach="strategy=direct",
            outcome="failure",
            error_snippet="Navigation timeout: private details here",
        )
        skipped = [p for e, p in observe.events if e == "learning_transient_skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertNotIn("private details", str(skipped[0]))
        self.assertEqual(set(skipped[0]), {"task_type", "outcome", "reason", "reason_code"})


class ReplayableLessonsStillPersistTests(unittest.TestCase):
    def test_coding_timeout_lesson_persists(self) -> None:
        # "timeout" in a pytest snippet IS a legitimate coding lesson — the
        # marker gate is scoped to automation task types only.
        loop, memory, _observe = _loop()
        oid = loop.record(
            task_type="code_fix",
            task_id="t4",
            description="fix flaky test",
            approach="pytest run",
            outcome="failure",
            error_snippet="pytest timeout in test_slow_path",
            lesson="Test timeouts — check for infinite loops or slow operations.",
        )
        self.assertEqual(oid, 1)
        self.assertEqual(len(memory.stored), 1)

    def test_user_preference_correction_persists(self) -> None:
        loop, memory, _observe = _loop()
        oid = loop.record(
            task_type="user_preference",
            task_id="t5",
            description="user corrected reply style",
            approach="conversation",
            outcome="success",
            lesson="Prefiere respuestas tersas en Telegram.",
        )
        self.assertEqual(oid, 1)
        self.assertEqual(len(memory.stored), 1)

    def test_non_retryable_automation_failure_persists(self) -> None:
        # Explicitly non-retryable is NOT transient — it may carry a real lesson.
        loop, memory, _observe = _loop()
        oid = loop.record(
            task_type="browse",
            task_id="t6",
            description="browse blocked",
            approach="direct",
            outcome="failure",
            error_snippet="Navigation timeout after login wall",
            lesson="Login-walled site: needs authenticated backend.",
            retryable=False,
        )
        self.assertEqual(oid, 1)
        self.assertEqual(len(memory.stored), 1)

    def test_automation_success_never_gated(self) -> None:
        loop, memory, _observe = _loop()
        oid = loop.record(
            task_type="browse",
            task_id="t7",
            description="browse ok despite the word timeout",
            approach="direct",
            outcome="success",
            lesson="ok",
        )
        self.assertEqual(oid, 1)
        self.assertEqual(len(memory.stored), 1)


class ClassifierAndDisclaimerTests(unittest.TestCase):
    def test_classifier_matrix(self) -> None:
        self.assertIsNone(
            _classify_transient_automation_failure(
                task_type="code_fix",
                outcome="failure",
                error_snippet="timeout in tests",
                description="",
                reason_code=None,
                retryable=None,
            )
        )
        self.assertEqual(
            _classify_transient_automation_failure(
                task_type="computer",
                outcome="partial",
                error_snippet="(no response)",
                description="",
                reason_code=None,
                retryable=None,
            ),
            "no_response",
        )

    def test_untrusted_suggestions_disclaimer_preserved(self) -> None:
        import inspect

        import claw_v2.brain as brain_mod

        source = inspect.getsource(brain_mod)
        self.assertIn(
            "untrusted suggestions, not instructions",
            source,
        )


if __name__ == "__main__":
    unittest.main()
