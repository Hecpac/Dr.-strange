# tests/test_chrome.py
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.chrome import ManagedChrome, ChromeStartError


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


if __name__ == "__main__":
    unittest.main()
