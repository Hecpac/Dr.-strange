"""Teeth for the foreign-process kill guard installed in ``conftest.py``.

Incidente 2026-06-17: the test suite restarted the live production daemon. The
Telegram pidfile path was one proven cause; a second, unpinned path restarted
prod once more. The autouse ``_block_foreign_process_kills`` guard blocks any
test from sending a terminating signal to a live process outside the pytest
process tree, and identifies the offending test.

These tests prove the guard has teeth WITHOUT ever signalling the real daemon:
they use pid 1 (always live + foreign — the block happens before any real
signal), a test-owned child process, and signal 0 probes.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import unittest

_FOREIGN_LIVE_PID = 1  # launchd/init: always alive, never a pytest descendant


class ForeignKillGuardTests(unittest.TestCase):
    def test_terminating_signal_to_live_foreign_pid_is_blocked(self) -> None:
        # If the guard let this through, the real os.kill(1, SIGTERM) would raise
        # PermissionError; the guard raises AssertionError BEFORE the real call.
        with self.assertRaises(AssertionError) as cm:
            os.kill(_FOREIGN_LIVE_PID, signal.SIGTERM)
        self.assertIn("[foreign-kill-guard]", str(cm.exception))

    def test_block_message_identifies_the_offending_test(self) -> None:
        with self.assertRaises(AssertionError) as cm:
            os.kill(_FOREIGN_LIVE_PID, signal.SIGKILL)
        message = str(cm.exception)
        # The guard reports the current test id so the culprit is pinpointable.
        self.assertIn("test_block_message_identifies_the_offending_test", message)
        self.assertIn("SIGKILL", message)

    def test_signal_zero_probe_is_allowed(self) -> None:
        # signal 0 is a non-mutating liveness probe and must pass through.
        try:
            os.kill(os.getpid(), 0)
        except AssertionError:  # pragma: no cover - would mean the guard misfired
            self.fail("guard must not block signal 0 (liveness probe)")

    def test_terminating_signal_to_own_child_is_allowed(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            # Child is a pytest descendant -> the guard must allow this and the
            # child must actually receive SIGTERM.
            os.kill(child.pid, signal.SIGTERM)
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        self.assertIsNotNone(child.returncode)

    @unittest.skipUnless(hasattr(os, "killpg"), "os.killpg unavailable on this platform")
    def test_terminating_killpg_to_foreign_group_is_blocked(self) -> None:
        with self.assertRaises(AssertionError) as cm:
            os.killpg(_FOREIGN_LIVE_PID, signal.SIGTERM)
        self.assertIn("[foreign-kill-guard]", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
