from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.eval import EvalCase, EvalHarness
from claw_v2.eval_mocks import build_test_router

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


if __name__ == "__main__":
    unittest.main()
