from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from claw_v2.langgraph_coordinator import _f6_shadow_metadata


class F6ShadowMetadataRedactionTests(unittest.TestCase):
    def test_fan_out_and_fan_in_redact_task_instructions(self) -> None:
        # issue #153: F6 shadow metadata copied WorkerTask.instructions verbatim,
        # leaking secrets into persisted observability. The instruction must be
        # redacted at the source so both fan_out_units and fan_in_results carry
        # the scrubbed form.
        secret = "sk-proj-abcdefghijklmnopqrstuvwx"
        task = SimpleNamespace(
            name="research-0",
            instruction=f"investiga usando la clave {secret}",
            lane="research",
        )
        legacy_result = SimpleNamespace(
            phase_results={
                "research": [
                    SimpleNamespace(
                        task_name="research-0",
                        error="",
                        content="resumen ok",
                        duration_seconds=1.0,
                    )
                ]
            }
        )

        meta = _f6_shadow_metadata(research_tasks=(task,), legacy_result=legacy_result)

        blob = json.dumps(meta)
        self.assertNotIn(secret, blob)
        self.assertIn("[REDACTED]", meta["fan_out_units"][0]["input"])
        # fan_in copies unit["input"], so it must already be redacted too.
        self.assertNotIn(secret, meta["fan_in_results"][0]["input"])


if __name__ == "__main__":
    unittest.main()
