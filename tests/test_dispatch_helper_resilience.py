"""C3: Resilience of module-level dispatch helpers in `claw_v2.bot`.

`_looks_like_operator_action_request` calls `detect_telegram_imperative`
and `detect_owner_delegation` inside a `try/except Exception: pass` block.
A detector raising must NOT vanish — the dispatch routing decision is
otherwise made on degraded signal with no audit. We assert that the
exception is logged at ERROR level (via `logger.exception`) so an
observer reading the standard logging stream sees it.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from claw_v2.bot import _looks_like_operator_action_request


class DispatchDetectorResilienceTests(unittest.TestCase):
    def test_telegram_imperative_detector_failure_is_logged(self) -> None:
        with patch(
            "claw_v2.bot.detect_telegram_imperative",
            side_effect=RuntimeError("imperative detector boom"),
        ):
            with self.assertLogs("claw_v2.bot", level="ERROR") as captured:
                _looks_like_operator_action_request("haz X mañana")
        joined = "\n".join(captured.output)
        self.assertIn("RuntimeError", joined)
        self.assertIn("imperative detector boom", joined)

    def test_owner_delegation_detector_failure_is_logged(self) -> None:
        with patch(
            "claw_v2.bot.detect_owner_delegation",
            side_effect=RuntimeError("delegation detector boom"),
        ):
            with self.assertLogs("claw_v2.bot", level="ERROR") as captured:
                _looks_like_operator_action_request("encárgate tú")
        joined = "\n".join(captured.output)
        self.assertIn("RuntimeError", joined)
        self.assertIn("delegation detector boom", joined)


if __name__ == "__main__":
    unittest.main()
