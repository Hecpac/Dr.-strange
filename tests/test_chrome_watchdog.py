"""Tests for chrome.py CDP watchdog (P0 hotfix C).

When CDP does not become ready in time and the profile is held by stale
PIDs, the watchdog must kill ONLY those PIDs (never regular user Chrome
running a different profile), retry the launch once, and never silently
"proceed anyway" if CDP still does not come up.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from claw_v2.chrome import ChromeStartError, ManagedChrome


class _WatchdogHarness(unittest.TestCase):
    def setUp(self) -> None:
        # _find_chrome → fixed path
        self._find_chrome_patcher = patch(
            "claw_v2.chrome._find_chrome",
            return_value="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
        self._find_chrome_patcher.start()
        self.addCleanup(self._find_chrome_patcher.stop)
        # _check_port_pids → always empty (no port conflict)
        self._port_pids_patcher = patch("claw_v2.chrome._check_port_pids", return_value=[])
        self._port_pids_patcher.start()
        self.addCleanup(self._port_pids_patcher.stop)
        # _wait_for_port_free no-op
        self._wait_port_free_patcher = patch("claw_v2.chrome._wait_for_port_free")
        self._wait_port_free_patcher.start()
        self.addCleanup(self._wait_port_free_patcher.stop)
        # _wait_for_profile_free no-op (avoid wait loops)
        self._wait_profile_free_patcher = patch("claw_v2.chrome._wait_for_profile_free")
        self._wait_profile_free_patcher.start()
        self.addCleanup(self._wait_profile_free_patcher.stop)
        self._focus_visible_chrome_patcher = patch("claw_v2.chrome._focus_visible_chrome")
        self._focus_visible_chrome_patcher.start()
        self.addCleanup(self._focus_visible_chrome_patcher.stop)


class ChromeWatchdogKillsOnlyExpectedProfilePidsTests(_WatchdogHarness):
    def test_chrome_watchdog_kills_only_expected_profile_pids(self) -> None:
        # Two PIDs hold the SAME profile we want to launch. We must kill
        # both — and only both.
        profile_pids_sequence = [[5555, 6666], []]

        kill_calls: list[int] = []

        def fake_kill(pid: int) -> None:
            kill_calls.append(pid)

        with (
            patch("claw_v2.chrome._profile_user_data_pids", side_effect=profile_pids_sequence),
            patch("claw_v2.chrome._kill_pid", side_effect=fake_kill),
            patch("claw_v2.chrome._is_cdp_ready", return_value=False),
            patch("claw_v2.chrome._wait_for_cdp_ready"),
            patch("subprocess.Popen") as mock_popen,
        ):
            proc = MagicMock()
            proc.poll.return_value = None
            mock_popen.return_value = proc
            mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
            mc.start()

        self.assertEqual(sorted(kill_calls), [5555, 6666])


class ChromeWatchdogDoesNotKillRegularUserChromeTests(_WatchdogHarness):
    def test_chrome_watchdog_does_not_kill_regular_user_chrome(self) -> None:
        # The user's regular Chrome runs with a DIFFERENT profile, so
        # _profile_user_data_pids returns []. No PID should be killed
        # (we may not touch other people's Chrome).
        kill_calls: list[int] = []

        def fake_kill(pid: int) -> None:
            kill_calls.append(pid)

        with (
            patch("claw_v2.chrome._profile_user_data_pids", return_value=[]),
            patch("claw_v2.chrome._kill_pid", side_effect=fake_kill),
            patch("claw_v2.chrome._is_cdp_ready", return_value=False),
            patch("claw_v2.chrome._wait_for_cdp_ready"),
            patch("subprocess.Popen") as mock_popen,
        ):
            proc = MagicMock()
            proc.poll.return_value = None
            mock_popen.return_value = proc
            mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
            mc.start()

        self.assertEqual(kill_calls, [])


class CdpNotReadyDoesNotProceedAnywayTests(_WatchdogHarness):
    def test_cdp_not_ready_does_not_proceed_anyway(self) -> None:
        # Profile is held by a stale PID; watchdog kills it and retries.
        # If CDP still does not come up, the launch must raise (never
        # silently fall through with a warning).
        events: list[tuple[str, dict]] = []

        def fake_observe(event_type: str, payload: dict) -> None:
            events.append((event_type, payload))

        with (
            patch("claw_v2.chrome._profile_user_data_pids", side_effect=[[7777], []]),
            patch("claw_v2.chrome._kill_pid"),
            patch("claw_v2.chrome._is_cdp_ready", return_value=False),
            patch(
                "claw_v2.chrome._wait_for_cdp_ready",
                side_effect=ChromeStartError("CDP timeout"),
            ),
            patch("subprocess.Popen") as mock_popen,
        ):
            proc = MagicMock()
            proc.poll.return_value = None
            mock_popen.return_value = proc
            mc = ManagedChrome(
                port=9250,
                profile_dir="/tmp/test-profile",
                observe=fake_observe,
            )
            with self.assertRaises(ChromeStartError):
                mc.start()

        event_types = [event_type for event_type, _ in events]
        self.assertIn("cdp_unavailable", event_types)


if __name__ == "__main__":
    unittest.main()
