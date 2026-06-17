"""Regression + guard: the test suite must never touch the production Telegram
process-ownership files.

Incidente 2026-06-17 (diagnosis): `pytest tests/` — run by the Claude Code
SessionStart hook on every session — exercised the real
``TelegramTransport.start()``. Without isolation, ``_PID_FILE`` resolves to the
production ``~/.claw/telegram.pid``; ``start()`` reads it, confirms the live
daemon via ``ps``, and sends it ``SIGTERM`` (single-instance poller takeover).
launchd ``KeepAlive`` then relaunched the daemon (clean exit 0) — so the suite
was silently restarting production on every run. Root cause was a test-isolation
defect, NOT RAÍZ #1 and NOT an F1 regression.

The autouse session guard ``_isolate_telegram_pidfiles_from_production`` in
``conftest.py`` repoints both Telegram ownership paths at a temp dir. These
tests:
  1-2. assert the guard is active (would have FAILED on the pre-fix setup,
       where the paths were the real ``~/.claw`` ones), and
  3-4. prove ``start()`` only ever acts on the isolated pidfile and can never
       ``os.kill`` the live daemon (``os.kill`` is spied — no real signals).
"""

from __future__ import annotations

import signal
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import claw_v2.telegram as telegram_mod
from claw_v2.telegram import PollingLockConflict, TelegramTransport, _polling_lock_path

_PROD_CLAW_DIR = Path.home() / ".claw"
_PROD_PID_FILE = _PROD_CLAW_DIR / "telegram.pid"


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


class TelegramIsolationGuardTests(unittest.TestCase):
    """Tripwire: fails loudly if the autouse isolation guard is removed/broken.

    On the pre-fix tree these assertions fail because ``_PID_FILE`` /
    ``_DEFAULT_POLLING_LOCK_DIR`` were the real ``~/.claw`` paths.
    """

    def test_pidfile_is_not_the_production_path(self) -> None:
        self.assertNotEqual(TelegramTransport._PID_FILE, _PROD_PID_FILE)
        self.assertFalse(
            _is_under(TelegramTransport._PID_FILE, _PROD_CLAW_DIR),
            f"Telegram pidfile must not live under {_PROD_CLAW_DIR} during tests; "
            f"got {TelegramTransport._PID_FILE}",
        )

    def test_polling_lock_dir_is_not_the_production_path(self) -> None:
        self.assertNotEqual(telegram_mod._DEFAULT_POLLING_LOCK_DIR, _PROD_CLAW_DIR)
        lock_path = _polling_lock_path("any-token")
        self.assertFalse(
            _is_under(lock_path, _PROD_CLAW_DIR),
            f"Telegram polling lock must not live under {_PROD_CLAW_DIR} during tests; "
            f"got {lock_path}",
        )


class TelegramStartCannotSignalProductionTests(unittest.IsolatedAsyncioTestCase):
    """``start()`` must only ever act on the isolated pidfile, never the real one."""

    async def test_start_with_empty_isolated_pidfile_signals_nobody(self) -> None:
        # Mirrors the previously-unsafe TransportStartTests path: no stale pid in
        # the isolated pidfile -> start() must not signal anyone.
        TelegramTransport._PID_FILE.unlink(missing_ok=True)
        kill_calls: list[tuple[int, int]] = []

        transport = TelegramTransport(bot_service=MagicMock(), token="test-token")
        with (
            patch("os.kill", lambda pid, sig: kill_calls.append((pid, sig))),
            patch.object(
                telegram_mod,
                "acquire_polling_lock",
                side_effect=PollingLockConflict(999999, Path("/tmp/fake.lock")),
            ),
            patch.object(telegram_mod, "ApplicationBuilder", MagicMock()),
        ):
            await transport.start()

        self.assertEqual(kill_calls, [], "start() signalled a process despite an empty pidfile")

    async def test_start_only_acts_on_isolated_pidfile_never_production(self) -> None:
        # Plant a sentinel pid ONLY in the isolated pidfile and make `ps` claim it
        # is a live claw_v2.main process. If start() read the real
        # ~/.claw/telegram.pid instead, the SIGTERM target would be the real
        # daemon pid, not our sentinel.
        sentinel_pid = 4242424
        TelegramTransport._PID_FILE.write_text(str(sentinel_pid))
        self.addCleanup(TelegramTransport._PID_FILE.unlink, missing_ok=True)

        kill_calls: list[tuple[int, int]] = []

        def kill_spy(pid: int, sig: int) -> None:
            kill_calls.append((pid, sig))
            if sig == 0:
                # Pretend the (fake) target is already gone so start()'s wait
                # loop breaks immediately instead of sleeping.
                raise ProcessLookupError

        fake_ps = AsyncMock(
            return_value=SimpleNamespace(stdout="/x/.venv/bin/python -m claw_v2.main", returncode=0)
        )

        transport = TelegramTransport(bot_service=MagicMock(), token="test-token")
        with (
            patch("os.kill", kill_spy),
            patch.object(telegram_mod, "run_subprocess_bounded_off_loop", fake_ps),
            patch.object(
                telegram_mod,
                "acquire_polling_lock",
                side_effect=PollingLockConflict(999999, Path("/tmp/fake.lock")),
            ),
            patch.object(telegram_mod, "ApplicationBuilder", MagicMock()),
        ):
            await transport.start()

        # `ps` was asked about the sentinel from the ISOLATED pidfile, not prod.
        ps_argv = fake_ps.await_args.args[0]
        self.assertIn(str(sentinel_pid), ps_argv)
        # The only SIGTERM went to the sentinel pid (never the real daemon).
        sigterms = [pid for pid, sig in kill_calls if sig == signal.SIGTERM]
        self.assertEqual(sigterms, [sentinel_pid])


if __name__ == "__main__":
    unittest.main()
