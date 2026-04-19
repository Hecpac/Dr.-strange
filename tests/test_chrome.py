# tests/test_chrome.py
from __future__ import annotations

import subprocess
import unittest
from unittest.mock import MagicMock, patch, call

from claw_v2.chrome import ManagedChrome, ChromeStartError


class ManagedChromeTests(unittest.TestCase):
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
    @patch("claw_v2.chrome._should_launch_headed_via_open")
    @patch("subprocess.Popen")
    @patch("claw_v2.chrome._wait_for_cdp_ready")
    def test_start_headless_false(self, mock_ready, mock_popen, mock_headed_open, mock_wait, mock_pids) -> None:
        mock_pids.return_value = []
        mock_headed_open.return_value = False
        proc = MagicMock()
        proc.poll.return_value = None
        mock_popen.return_value = proc
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        mc.start(headless=False)
        args = mock_popen.call_args[0][0]
        self.assertNotIn("--headless=new", args)

    @patch("claw_v2.chrome._check_port_pids")
    @patch("claw_v2.chrome._find_chrome")
    @patch("claw_v2.chrome._wait_for_port_free")
    @patch("claw_v2.chrome._should_launch_headed_via_open")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    @patch("claw_v2.chrome._wait_for_cdp_ready")
    def test_start_headed_macos_uses_open(
        self,
        mock_ready,
        mock_popen,
        mock_run,
        mock_headed_open,
        mock_wait,
        mock_find_chrome,
        mock_pids,
    ) -> None:
        mock_pids.return_value = []
        mock_find_chrome.return_value = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        mock_headed_open.return_value = True
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        mc.start(headless=False)
        mock_popen.assert_not_called()
        args = mock_run.call_args[0][0]
        self.assertEqual(args[:4], ["open", "-na", "Google Chrome", "--args"])
        self.assertIn("--remote-debugging-port=9250", args)
        self.assertIn("--user-data-dir=/tmp/test-profile", args)
        self.assertNotIn("--headless=new", args)

    @patch("claw_v2.chrome._is_cdp_ready")
    def test_is_running_uses_cdp_without_child_process(self, mock_ready) -> None:
        mock_ready.return_value = True
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        self.assertTrue(mc.is_running)

    @patch("claw_v2.chrome._check_port_pids")
    @patch("claw_v2.chrome._kill_pid")
    def test_stop_kills_chrome_port_owner_without_child_process(self, mock_kill, mock_pids) -> None:
        mock_pids.return_value = [(1234, "Google Chrome")]
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        mc.stop()
        mock_kill.assert_called_once_with(1234)


if __name__ == "__main__":
    unittest.main()
