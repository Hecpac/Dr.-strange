from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.eval import EvalCase, EvalHarness
from claw_v2.eval_mocks import build_test_router
from claw_v2.hooks import make_decision_logger
from claw_v2.observe import ObserveStream

from tests.helpers import make_config


class EvalHarnessTests(unittest.TestCase):
    def test_run_case_validates_expected_and_forbidden_substrings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            router = build_test_router(config)
            harness = EvalHarness(router)
            result = harness.run_case(
                EvalCase(
                    name="judge-basic",
                    prompt="classify this",
                    lane="judge",
                    expected_substrings=("openai:judge",),
                    forbidden_substrings=("error",),
                    evidence_pack={"kind": "unit"},
                )
            )
            self.assertTrue(result.passed)

    def test_run_case_can_compare_expected_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            router = build_test_router(config)
            harness = EvalHarness(router)
            result = harness.run_case(
                EvalCase(
                    name="judge-exact",
                    prompt="classify this",
                    lane="judge",
                    expected_response="openai:judge:gpt-5.4-mini",
                    evidence_pack={"kind": "unit"},
                )
            )
            self.assertTrue(result.passed)

    def test_capture_save_load_and_replay_trace_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = make_config(root)
            observe = ObserveStream(root / "observe.db")

            def audit_sink(event: dict) -> None:
                observe.emit(
                    event["action"],
                    lane=event["lane"],
                    provider=event["provider"],
                    model=event["model"],
                    trace_id=event["metadata"].get("trace_id"),
                    root_trace_id=event["metadata"].get("root_trace_id"),
                    span_id=event["metadata"].get("span_id"),
                    parent_span_id=event["metadata"].get("parent_span_id"),
                    job_id=event["metadata"].get("job_id"),
                    artifact_id=event["metadata"].get("artifact_id"),
                    payload={
                        "cost_estimate": event["cost_estimate"],
                        "response_text": event["metadata"].get("response_text"),
                    },
                )

            router = build_test_router(
                config,
                audit_sink=audit_sink,
                post_hooks=[make_decision_logger(observe)],
            )
            harness = EvalHarness(router)

            response = router.ask(
                "classify this",
                lane="judge",
                evidence_pack={"kind": "unit", "artifact_id": "eval-trace"},
            )
            self.assertEqual(response.content, "openai:judge:gpt-5.4-mini")

            trace_id = observe.recent_events(limit=1)[0]["trace_id"]
            case = harness.capture_trace_case(observe, trace_id, name="captured")
            self.assertEqual(case.prompt, "classify this")
            self.assertEqual(case.expected_response, "openai:judge:gpt-5.4-mini")
            self.assertEqual(case.metadata["trace_id"], trace_id)

            saved = harness.save_case(case, root / "cases")
            loaded = harness.load_case(saved)
            replay = harness.run_case(loaded)
            self.assertTrue(replay.passed)


if __name__ == "__main__":
    unittest.main()
