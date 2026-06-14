# tests/test_chrome.py
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

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

        self._focus_visible_chrome_patcher = patch("claw_v2.chrome._focus_visible_chrome")
        self.mock_focus_visible_chrome = self._focus_visible_chrome_patcher.start()
        self.addCleanup(self._focus_visible_chrome_patcher.stop)

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
        self.assertIn("--remote-allow-origins=*", args)
        self.assertIn(f"--user-data-dir={Path('/tmp/test-profile').resolve(strict=False)}", args)
        self.assertNotIn("--headless=new", args)
        self.assertIn("--start-maximized", args)
        self.assertIn("--window-position=0,0", args)
        self.assertIn("--window-size=1440,1000", args)
        self.assertIn("--no-first-run", args)

    @patch("claw_v2.chrome._check_port_pids")
    @patch("claw_v2.chrome._kill_pid")
    @patch("claw_v2.chrome._wait_for_port_free")
    @patch("subprocess.Popen")
    @patch("claw_v2.chrome._wait_for_cdp_ready")
    def test_start_kills_existing_chrome(self, mock_ready, mock_popen, mock_wait, mock_kill, mock_pids) -> None:
        mock_pids.return_value = [(1234, "Google Chrome")]
        # PID 1234 holds OUR managed profile, so it is reclaimable. Subsequent
        # lookups (wait_for_profile_free, _reclaim_profile_if_busy) see it gone.
        self.mock_profile_user_data_pids.side_effect = [[1234], [], []]
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
    def test_start_headless_true(self, mock_ready, mock_popen, mock_wait, mock_pids) -> None:
        mock_pids.return_value = []
        proc = MagicMock()
        proc.poll.return_value = None
        mock_popen.return_value = proc
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        mc.start(headless=True)
        args = mock_popen.call_args[0][0]
        self.assertIn("--headless=new", args)
        self.assertNotIn("--start-maximized", args)

    @patch("claw_v2.chrome._check_port_pids")
    @patch("claw_v2.chrome._pid_is_headless", return_value=False)
    @patch("subprocess.Popen")
    def test_start_reuses_existing_ready_cdp_chrome(self, mock_popen, mock_is_headless, mock_pids) -> None:
        mock_pids.return_value = [(1234, "Google Chrome")]
        # The ready CDP Chrome is running OUR managed profile, so it is reused.
        self.mock_profile_user_data_pids.return_value = [1234]
        self.mock_cdp_ready.return_value = True
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        mc.start()
        mock_popen.assert_not_called()
        self.assertIsNone(mc._process)
        self.assertEqual(mc._attached_pid, 1234)
        self.mock_focus_visible_chrome.assert_called_once_with(pid=1234)

    @patch("claw_v2.chrome._check_port_pids")
    @patch("claw_v2.chrome._pid_is_headless", return_value=False)
    @patch("claw_v2.chrome._focus_existing_cdp_page", return_value=False)
    @patch("claw_v2.chrome._cdp_page_targets", return_value=[])
    @patch("claw_v2.chrome._open_cdp_target")
    @patch("subprocess.Popen")
    def test_start_creates_visible_tab_when_ready_cdp_has_no_window(
        self,
        mock_popen,
        mock_open_target,
        mock_page_targets,
        mock_focus_cdp,
        mock_is_headless,
        mock_pids,
    ) -> None:
        mock_pids.return_value = [(1234, "Google Chrome")]
        self.mock_profile_user_data_pids.return_value = [1234]
        self.mock_cdp_ready.return_value = True
        self.mock_focus_visible_chrome.side_effect = [False, True]
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")

        mc.start()

        mock_popen.assert_not_called()
        mock_open_target.assert_called_once_with(9250, "about:blank")
        self.assertEqual(
            self.mock_focus_visible_chrome.call_args_list,
            [call(pid=1234), call(pid=1234)],
        )

    @patch("claw_v2.chrome._check_port_pids")
    @patch("claw_v2.chrome._pid_is_headless", return_value=False)
    @patch("claw_v2.chrome._focus_existing_cdp_page", return_value=True)
    @patch("claw_v2.chrome._cdp_page_targets")
    @patch("claw_v2.chrome._open_cdp_target")
    @patch("subprocess.Popen")
    def test_start_focuses_existing_cdp_page_before_creating_new_tab(
        self,
        mock_popen,
        mock_open_target,
        mock_page_targets,
        mock_focus_cdp,
        mock_is_headless,
        mock_pids,
    ) -> None:
        mock_pids.return_value = [(1234, "Google Chrome")]
        self.mock_profile_user_data_pids.return_value = [1234]
        self.mock_cdp_ready.return_value = True
        self.mock_focus_visible_chrome.side_effect = [False, True]
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")

        mc.start()

        mock_popen.assert_not_called()
        mock_focus_cdp.assert_called_once_with(9250)
        mock_open_target.assert_not_called()
        mock_page_targets.assert_not_called()

    @patch("claw_v2.chrome._check_port_pids")
    @patch("claw_v2.chrome._pid_is_headless", return_value=False)
    @patch("claw_v2.chrome._focus_existing_cdp_page", return_value=False)
    @patch("claw_v2.chrome._cdp_page_targets", return_value=[{"type": "page", "url": "https://www.google.com/"}])
    @patch("claw_v2.chrome._open_cdp_target")
    @patch("subprocess.Popen")
    def test_start_does_not_create_extra_tab_when_ready_cdp_already_has_pages(
        self,
        mock_popen,
        mock_open_target,
        mock_page_targets,
        mock_focus_cdp,
        mock_is_headless,
        mock_pids,
    ) -> None:
        mock_pids.return_value = [(1234, "Google Chrome")]
        self.mock_profile_user_data_pids.return_value = [1234]
        self.mock_cdp_ready.return_value = True
        self.mock_focus_visible_chrome.return_value = False
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")

        mc.start()

        mock_popen.assert_not_called()
        mock_open_target.assert_not_called()
        mock_page_targets.assert_called_once_with(9250)

    @patch("claw_v2.chrome._check_port_pids")
    @patch("claw_v2.chrome._kill_pid")
    @patch("claw_v2.chrome._wait_for_port_free")
    @patch("claw_v2.chrome._pid_is_headless", return_value=True)
    @patch("subprocess.Popen")
    @patch("claw_v2.chrome._wait_for_cdp_ready")
    def test_start_relaunches_existing_headless_cdp_when_visible_requested(
        self,
        mock_ready,
        mock_popen,
        mock_is_headless,
        mock_wait_port,
        mock_kill,
        mock_pids,
    ) -> None:
        mock_pids.return_value = [(1234, "Google Chrome")]
        self.mock_profile_user_data_pids.side_effect = [[1234], [], []]
        self.mock_cdp_ready.return_value = True
        proc = MagicMock()
        proc.poll.return_value = None
        mock_popen.return_value = proc
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        mc.start()
        mock_kill.assert_called_with(1234)
        self.assertGreaterEqual(mock_wait_port.call_count, 1)
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        self.assertNotIn("--headless=new", args)

    @patch("claw_v2.chrome._check_port_pids")
    @patch("subprocess.Popen")
    def test_start_refuses_ready_cdp_chrome_with_different_profile(self, mock_popen, mock_pids) -> None:
        # A ready CDP Chrome on the port that is NOT our managed profile must
        # not be hijacked: refuse rather than attach to a foreign profile.
        mock_pids.return_value = [(1234, "Google Chrome")]
        self.mock_profile_user_data_pids.return_value = []
        self.mock_cdp_ready.return_value = True
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        with self.assertRaises(ChromeStartError) as ctx:
            mc.start()
        self.assertIn("different profile", str(ctx.exception))
        self.assertIn("/tmp/test-profile", str(ctx.exception))
        mock_popen.assert_not_called()

    @patch("claw_v2.chrome._check_port_pids")
    @patch("claw_v2.chrome._kill_pid")
    def test_start_refuses_to_kill_chrome_with_different_profile(self, mock_kill, mock_pids) -> None:
        # A Chrome on the port that is NOT our managed profile must not be
        # killed (it could be the user's own Chrome bound to that port).
        mock_pids.return_value = [(1234, "Google Chrome")]
        self.mock_profile_user_data_pids.return_value = []
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        with self.assertRaises(ChromeStartError) as ctx:
            mc.start()
        self.assertIn("different profile", str(ctx.exception))
        self.assertIn("9250", str(ctx.exception))
        mock_kill.assert_not_called()

    def test_profile_dir_is_canonicalized(self) -> None:
        # profile_dir is resolved so the path compared against ps --user-data-dir
        # (also resolved) matches even through a symlink.
        with tempfile.TemporaryDirectory() as tmpdir:
            real_profile = Path(tmpdir) / "real-profile"
            symlink_profile = Path(tmpdir) / "linked-profile"
            real_profile.mkdir()
            symlink_profile.symlink_to(real_profile, target_is_directory=True)

            mc = ManagedChrome(port=9250, profile_dir=str(symlink_profile))

            self.assertEqual(mc.profile_dir, str(real_profile.resolve(strict=False)))

    @patch("claw_v2.chrome._check_port_pids")
    @patch("claw_v2.chrome._wait_for_profile_free")
    @patch("claw_v2.chrome._kill_pid")
    @patch("claw_v2.chrome._wait_for_cdp_ready")
    @patch("subprocess.Popen")
    def test_start_reclaims_profile_and_retries_then_raises_when_cdp_persistently_unavailable(
        self,
        mock_popen,
        mock_wait_cdp,
        mock_kill,
        mock_wait_profile,
        mock_pids,
    ) -> None:
        # P0 hotfix C update: profile-held stale PIDs no longer raise on
        # first detection; the watchdog kills them and retries. If CDP
        # still does not come up, ChromeStartError is raised (no silent
        # "proceeding anyway").
        mock_pids.return_value = []
        self.mock_profile_user_data_pids.return_value = [4321]
        mock_wait_cdp.side_effect = ChromeStartError("CDP timeout")
        proc = MagicMock()
        proc.poll.return_value = None
        mock_popen.return_value = proc
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        with self.assertRaises(ChromeStartError):
            mc.start()
        mock_kill.assert_any_call(4321)

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
                patch("claw_v2.chrome._profile_user_data_pids", return_value=[9999]),
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
                patch("claw_v2.chrome._profile_user_data_pids", return_value=[1234]),
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
