# tests/test_chrome.py
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.chrome import ManagedChrome, ChromeStartError, _profile_user_data_pids


class ManagedChromeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._find_chrome_patcher = patch(
            "claw_v2.chrome._find_chrome",
            return_value="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
        self.mock_find_chrome = self._find_chrome_patcher.start()
        self.addCleanup(self._find_chrome_patcher.stop)

        self._cdp_ready_patcher = patch("claw_v2.chrome._is_cdp_ready", return_value=False)
        self.mock_cdp_ready = self._cdp_ready_patcher.start()
        self.addCleanup(self._cdp_ready_patcher.stop)

        self._profile_pids_patcher = patch("claw_v2.chrome._profile_user_data_pids", return_value=[])
        self.mock_profile_user_data_pids = self._profile_pids_patcher.start()
        self.addCleanup(self._profile_pids_patcher.stop)

    @patch("claw_v2.chrome._check_port_pids")
    @patch("claw_v2.chrome._wait_for_port_free")
    @patch("subprocess.Popen")
    @patch("claw_v2.chrome._wait_for_cdp_ready")
    def test_start_launches_chrome(self, mock_ready, mock_popen, mock_wait, mock_pids) -> None:
        mock_pids.return_value = []  # port free
        proc = MagicMock()
        proc.poll.return_value = None
        mock_popen.return_value = proc
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        mc.start()
        args = mock_popen.call_args[0][0]
        self.assertIn("--remote-debugging-port=9250", args)
        self.assertIn("--user-data-dir=/tmp/test-profile", args)
        self.assertIn("--headless=new", args)
        self.assertIn("--no-first-run", args)

    @patch("claw_v2.chrome._check_port_pids")
    @patch("claw_v2.chrome._kill_pid")
    @patch("claw_v2.chrome._wait_for_port_free")
    @patch("subprocess.Popen")
    @patch("claw_v2.chrome._wait_for_cdp_ready")
    def test_start_kills_existing_chrome(self, mock_ready, mock_popen, mock_wait, mock_kill, mock_pids) -> None:
        mock_pids.return_value = [(1234, "Google Chrome")]
        proc = MagicMock()
        proc.poll.return_value = None
        mock_popen.return_value = proc
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        mc.start()
        mock_kill.assert_called_once_with(1234)
        mock_wait.assert_called_once()

    @patch("claw_v2.chrome._check_port_pids")
    def test_start_errors_non_chrome_on_port(self, mock_pids) -> None:
        mock_pids.return_value = [(5678, "node")]
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        with self.assertRaises(ChromeStartError) as ctx:
            mc.start()
        self.assertIn("node", str(ctx.exception))
        self.assertIn("9250", str(ctx.exception))

    def test_stop_kills_subprocess(self) -> None:
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        proc = MagicMock()
        mc._process = proc
        mc.stop()
        proc.terminate.assert_called_once()
        self.assertIsNone(mc._process)

    @patch("claw_v2.chrome._check_port_pids")
    @patch("claw_v2.chrome._wait_for_port_free")
    @patch("subprocess.Popen")
    @patch("claw_v2.chrome._wait_for_cdp_ready")
    def test_ensure_idempotent(self, mock_ready, mock_popen, mock_wait, mock_pids) -> None:
        mock_pids.return_value = []
        proc = MagicMock()
        proc.poll.return_value = None  # alive
        mock_popen.return_value = proc
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        mc.start()
        mc.ensure()  # should not call Popen again
        self.assertEqual(mock_popen.call_count, 1)

    def test_custom_port(self) -> None:
        mc = ManagedChrome(port=9999, profile_dir="/tmp/p")
        self.assertEqual(mc.cdp_url, "http://localhost:9999")

    @patch("claw_v2.chrome._check_port_pids")
    @patch("claw_v2.chrome._wait_for_port_free")
    @patch("subprocess.Popen")
    @patch("claw_v2.chrome._wait_for_cdp_ready")
    def test_start_headless_false(self, mock_ready, mock_popen, mock_wait, mock_pids) -> None:
        mock_pids.return_value = []
        proc = MagicMock()
        proc.poll.return_value = None
        mock_popen.return_value = proc
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        mc.start(headless=False)
        args = mock_popen.call_args[0][0]
        self.assertNotIn("--headless=new", args)

    @patch("claw_v2.chrome._check_port_pids")
    @patch("subprocess.Popen")
    def test_start_reuses_existing_ready_cdp_chrome(self, mock_popen, mock_pids) -> None:
        mock_pids.return_value = [(1234, "Google Chrome")]
        self.mock_cdp_ready.return_value = True
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        mc.start()
        mock_popen.assert_not_called()
        self.assertIsNone(mc._process)

    @patch("claw_v2.chrome._check_port_pids")
    @patch("claw_v2.chrome._wait_for_profile_free")
    def test_start_errors_when_profile_active_without_ready_cdp(self, mock_wait_profile, mock_pids) -> None:
        mock_pids.return_value = []
        self.mock_profile_user_data_pids.return_value = [4321]
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        with self.assertRaises(ChromeStartError) as ctx:
            mc.start()
        self.assertIn("/tmp/test-profile", str(ctx.exception))
        self.assertIn("4321", str(ctx.exception))

    @patch("claw_v2.chrome._check_port_pids")
    @patch("subprocess.Popen")
    @patch("claw_v2.chrome._wait_for_cdp_ready")
    def test_start_removes_stale_singleton_lock(self, mock_ready, mock_popen, mock_pids) -> None:
        mock_pids.return_value = []
        proc = MagicMock()
        proc.poll.return_value = None
        mock_popen.return_value = proc
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = f"{tmpdir}/SingletonLock"
            with open(lock_path, "w", encoding="utf-8") as lock_file:
                lock_file.write("stale")
            mc = ManagedChrome(port=9250, profile_dir=tmpdir)
            mc.start()
            self.assertFalse(Path(lock_path).exists())


class ManagedChromeAttachStopTests(unittest.TestCase):
    # Regression for Bug #1: stop() was a no-op when start() attached to an existing
    # Chrome (self._process stayed None). /chrome_login relies on stop() to kill Chrome
    # before restarting visible.

    def _make_mc(self, tmpdir: str) -> ManagedChrome:
        return ManagedChrome(port=9250, profile_dir=tmpdir)

    def test_stop_kills_attached_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mc = self._make_mc(tmpdir)
            with (
                patch("claw_v2.chrome._check_port_pids", return_value=[(9999, "Google Chrome")]),
                patch("claw_v2.chrome._is_cdp_ready", return_value=True),
            ):
                mc.start()
            self.assertIsNone(mc._process)
            self.assertEqual(mc._attached_pid, 9999)

            killed: list[tuple[int, int]] = []
            def fake_kill(pid: int, sig: int) -> None:
                killed.append((pid, sig))

            with (
                patch("claw_v2.chrome._wait_for_port_free"),
                patch("os.kill", side_effect=[None, ProcessLookupError()]),
            ):
                mc.stop()

            self.assertIsNone(mc._attached_pid)

    def test_stop_is_not_noop_after_attach(self) -> None:
        """Without the fix, stop() would silently do nothing after attach."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mc = self._make_mc(tmpdir)
            with (
                patch("claw_v2.chrome._check_port_pids", return_value=[(1234, "Google Chrome")]),
                patch("claw_v2.chrome._is_cdp_ready", return_value=True),
            ):
                mc.start()
            self.assertEqual(mc._attached_pid, 1234)
            # After stop(), _attached_pid must be cleared (not None from the start)
            with (
                patch("claw_v2.chrome._wait_for_port_free"),
                patch("os.kill", side_effect=ProcessLookupError()),
            ):
                mc.stop()
            self.assertIsNone(mc._attached_pid)


class ProfileUserDataPidsTests(unittest.TestCase):
    # Regression for Bug #7: ps without -ww truncates long command lines on macOS,
    # causing --user-data-dir paths near/past col 80 to be missed.
    def test_detects_user_data_dir_in_long_command_line(self) -> None:
        profile = "/Users/hector/.claw/chrome-profile"
        padding = "x" * 200
        long_line = f"  1234 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --{padding} --user-data-dir={profile} --remote-debugging-port=9222"
        with patch("claw_v2.chrome.subprocess.check_output", return_value=long_line):
            pids = _profile_user_data_pids(profile)
        self.assertEqual(pids, [1234])

    def test_misses_user_data_dir_when_line_truncated(self) -> None:
        profile = "/Users/hector/.claw/chrome-profile"
        # Simulate what ps without -ww would return: line cut before the flag appears
        truncated = f"  1234 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote"
        with patch("claw_v2.chrome.subprocess.check_output", return_value=truncated):
            pids = _profile_user_data_pids(profile)
        self.assertEqual(pids, [])


if __name__ == "__main__":
    unittest.main()
