from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from claw_v2.browser_profiles import (
    BROWSER_PROFILES,
    BrowserProfileHealth,
    check_profile_health,
    classify_health,
    human_message,
    resolve_profile_for_objective,
)
from claw_v2.computer_handler import ComputerHandler
from claw_v2.observe import ObserveStream

_X = BROWSER_PROFILES["x"]


class ClassifyHealthTests(unittest.TestCase):
    def test_ok_when_logged_in_timeline(self) -> None:
        self.assertEqual(
            classify_health(
                final_url="https://x.com/home",
                title="Home / X",
                body_text="For you ... Post your reply",
                profile=_X,
            ),
            BrowserProfileHealth.OK,
        )

    def test_needs_login_when_login_flow(self) -> None:
        self.assertEqual(
            classify_health(
                final_url="https://x.com/i/flow/login",
                title="Sign in to X",
                body_text="Sign in to X",
                profile=_X,
            ),
            BrowserProfileHealth.NEEDS_LOGIN,
        )

    def test_challenge_detected(self) -> None:
        self.assertEqual(
            classify_health(
                final_url="https://x.com/",
                title="Just a moment...",
                body_text="Verifying you are human. cf-challenge",
                profile=_X,
            ),
            BrowserProfileHealth.BLOCKED_BY_CHALLENGE,
        )

    def test_challenge_wins_over_login(self) -> None:
        # A challenge wall can also look logged-out; challenge must win.
        self.assertEqual(
            classify_health(
                final_url="https://x.com/i/flow/login",
                title="Just a moment...",
                body_text="checking your browser ... sign in to x",
                profile=_X,
            ),
            BrowserProfileHealth.BLOCKED_BY_CHALLENGE,
        )


class ResolveProfileTests(unittest.TestCase):
    def test_x_objective_resolves_to_x(self) -> None:
        self.assertEqual(resolve_profile_for_objective("Haz un repaso por X").name, "x")

    def test_non_x_browse_resolves_to_none(self) -> None:
        self.assertIsNone(resolve_profile_for_objective("abre https://example.com y lee el h1"))

    def test_blank_resolves_to_none(self) -> None:
        self.assertIsNone(resolve_profile_for_objective(""))


class CheckProfileHealthTests(unittest.TestCase):
    def test_prober_ok(self) -> None:
        health, _ = check_profile_health(
            _X, "cdp", prober=lambda c, h, t: ("https://x.com/home", "Home / X", "timeline")
        )
        self.assertEqual(health, BrowserProfileHealth.OK)

    def test_prober_needs_login(self) -> None:
        health, _ = check_profile_health(
            _X,
            "cdp",
            prober=lambda c, h, t: ("https://x.com/i/flow/login", "Sign in to X", "sign in to x"),
        )
        self.assertEqual(health, BrowserProfileHealth.NEEDS_LOGIN)

    def test_prober_failure_is_unavailable(self) -> None:
        def boom(c, h, t):
            raise RuntimeError("cdp down")

        health, detail = check_profile_health(_X, "cdp", prober=boom)
        self.assertEqual(health, BrowserProfileHealth.UNAVAILABLE)
        self.assertIn("cdp down", detail)


class HumanMessageTests(unittest.TestCase):
    def test_messages_present_and_non_evasive(self) -> None:
        login = human_message(_X, BrowserProfileHealth.NEEDS_LOGIN)
        chal = human_message(_X, BrowserProfileHealth.BLOCKED_BY_CHALLENGE)
        self.assertIn("deslogueado", login.lower())
        self.assertIn("verificación", chal.lower())
        self.assertIn("no intento evadirlo", chal.lower())


class _GateHarness(unittest.TestCase):
    def _handler(self, root: Path):
        observe = ObserveStream(root / "observe.db")
        return ComputerHandler(observe=observe), observe


class BrowserProfileGateTests(_GateHarness):
    def test_non_x_objective_skips_health_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler, _ = self._handler(Path(tmp))
            with patch("claw_v2.browser_profiles.check_profile_health") as chk:
                out = handler._browser_profile_gate("abre example.com", "cdp", task_id="t")
            self.assertIsNone(out)
            chk.assert_not_called()

    def test_x_ok_proceeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler, observe = self._handler(Path(tmp))
            with patch(
                "claw_v2.browser_profiles.check_profile_health",
                return_value=(BrowserProfileHealth.OK, ""),
            ):
                out = handler._browser_profile_gate("repaso por X", "cdp", task_id="t")
            self.assertIsNone(out)
            types = [e["event_type"] for e in observe.recent_events(limit=20)]
            self.assertIn("browser_profile_health_checked", types)
            self.assertNotIn("browser_profile_needs_login", types)

    def test_x_needs_login_blocks_with_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler, observe = self._handler(Path(tmp))
            with patch(
                "claw_v2.browser_profiles.check_profile_health",
                return_value=(BrowserProfileHealth.NEEDS_LOGIN, ""),
            ):
                out = handler._browser_profile_gate("repaso por X", "cdp", task_id="t")
            self.assertIsNotNone(out)
            self.assertIn("deslogueado", out.lower())
            types = [e["event_type"] for e in observe.recent_events(limit=20)]
            self.assertIn("browser_profile_needs_login", types)

    def test_x_challenge_blocks_with_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler, observe = self._handler(Path(tmp))
            with patch(
                "claw_v2.browser_profiles.check_profile_health",
                return_value=(BrowserProfileHealth.BLOCKED_BY_CHALLENGE, ""),
            ):
                out = handler._browser_profile_gate("repaso por X", "cdp", task_id="t")
            self.assertIsNotNone(out)
            self.assertIn("verificación", out.lower())
            types = [e["event_type"] for e in observe.recent_events(limit=20)]
            self.assertIn("browser_profile_blocked_by_challenge", types)


class RunDelegatedGateWiringTests(_GateHarness):
    def _wire(self, handler):
        # Make the CDP layer trivially "ready" so run_delegated reaches the gate.
        handler._get_browser_capability = lambda: SimpleNamespace(
            ensure_ready=lambda port, profile_dir: "http://127.0.0.1:9250"
        )

    def test_blocked_profile_short_circuits_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler, _ = self._handler(Path(tmp))
            self._wire(handler)
            ran = {"agent": False}
            handler._run_browser_use_task = lambda *a, **k: (
                ran.__setitem__("agent", True) or "AGENT"
            )
            with patch.object(handler, "_browser_profile_gate", return_value="LOGIN NEEDED"):
                out = handler.run_delegated_browser_task("repaso por X", task_id="t", mode="browse")
            self.assertEqual(out, "LOGIN NEEDED")
            self.assertFalse(ran["agent"])  # X profile blocked -> agent never ran

    def test_ok_profile_proceeds_to_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler, _ = self._handler(Path(tmp))
            self._wire(handler)
            handler.browser_use = SimpleNamespace(cdp_url="http://127.0.0.1:9250")
            handler._ensure_browser_use_service = lambda endpoint: None
            handler._set_browser_use_cdp_url = lambda endpoint: None
            handler._run_browser_use_task = lambda *a, **k: "AGENT RAN"
            with patch.object(handler, "_browser_profile_gate", return_value=None):
                out = handler.run_delegated_browser_task("repaso por X", task_id="t", mode="browse")
            self.assertEqual(out, "AGENT RAN")


if __name__ == "__main__":
    unittest.main()
