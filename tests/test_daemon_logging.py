from __future__ import annotations

import io
import logging
import unittest


class DaemonLoggingConfigTests(unittest.TestCase):
    """Slice 4 (blind-spot pass #4): the daemon configures its own logging so
    boot leaves a legible stderr trace independent of browser_use, at WARNING
    (not INFO) to avoid per-request spam and secret leakage to the file."""

    def setUp(self) -> None:
        self._root = logging.getLogger()
        self._saved_handlers = self._root.handlers[:]
        self._saved_level = self._root.level

    def tearDown(self) -> None:
        self._root.handlers[:] = self._saved_handlers
        self._root.setLevel(self._saved_level)

    def test_configure_installs_warning_root_handler(self) -> None:
        from claw_v2.main import configure_daemon_logging

        self._root.handlers[:] = []  # simulate a fresh process (no handlers)
        configure_daemon_logging()

        self.assertTrue(self._root.handlers, "a root handler must be installed")
        self.assertEqual(self._root.level, logging.WARNING)

    def test_info_is_not_emitted_but_warning_is(self) -> None:
        # INFO must stay silent (no spam / no secret leak); WARNING+ reaches
        # stderr — the boot marker and tracebacks are WARNING/ERROR.
        from claw_v2.main import configure_daemon_logging

        self._root.handlers[:] = []
        configure_daemon_logging()
        buf = io.StringIO()
        self._root.handlers[0].stream = buf  # redirect the installed handler

        log = logging.getLogger("claw_v2.test_slice4")
        log.info("this INFO line must not appear (would risk secrets)")
        log.warning("Claw boot complete: pid=123 web_port=8765")

        out = buf.getvalue()
        self.assertNotIn("must not appear", out)
        self.assertIn("Claw boot complete", out)


class BootCompleteMarkerTests(unittest.TestCase):
    def test_boot_complete_line_is_redaction_safe_and_greppable(self) -> None:
        # The lifecycle marker carries only pid + port — no secret-shaped
        # fields — so a positive boot signal on stderr never leaks credentials.
        import inspect

        from claw_v2 import lifecycle

        src = inspect.getsource(lifecycle.run)
        self.assertIn("Claw boot complete", src)
        self.assertIn("pid=%s web_port=%s", src)
        # Must not interpolate any token/secret config into the marker.
        marker_line = next(line for line in src.splitlines() if "Claw boot complete" in line)
        for banned in ("token", "secret", "key", "password"):
            self.assertNotIn(banned, marker_line.lower())


if __name__ == "__main__":
    unittest.main()
