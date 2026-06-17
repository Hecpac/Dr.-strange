"""Suite-wide guards.

T10 (incidente 2026-06-12): the daemon's WAL sidecars were unlinked under its
live connections while `pytest tests/` ran from the production repo root —
any test that builds AppConfig without overriding DB_PATH resolves the
RELATIVE default `data/claw.db` and pokes the live database (a short-lived
external SQLite connection closing against it is enough to delete the
sidecars and wedge every daemon writer with `database is locked`).

The autouse session guard below redirects the DB_PATH fallback to a temp dir
so the suite can never touch the production database, no matter the cwd.
Tests that set their own DB_PATH (almost all do, via patch.dict) are
unaffected.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_runtime_db_from_production():
    if os.environ.get("DB_PATH"):
        yield
        return
    with tempfile.TemporaryDirectory(prefix="claw-test-db-isolation-") as tmpdir:
        os.environ["DB_PATH"] = str(Path(tmpdir) / "claw.db")
        try:
            yield
        finally:
            os.environ.pop("DB_PATH", None)


@pytest.fixture(autouse=True, scope="session")
def _isolate_telegram_pidfiles_from_production():
    """Keep the suite off the production Telegram process-ownership files.

    Incidente 2026-06-17: `pytest tests/` (run by the Claude Code SessionStart
    hook on every session) exercised the real ``TelegramTransport.start()``.
    With no override, ``_PID_FILE`` resolves to the production
    ``~/.claw/telegram.pid``; ``start()`` reads it, confirms the live daemon via
    ``ps``, and sends it ``SIGTERM`` (the single-instance poller takeover at
    ``telegram.py``). launchd ``KeepAlive`` then relaunched the daemon — so the
    test suite was silently restarting production on every run (clean exit 0,
    not RAÍZ #1).

    This session guard repoints BOTH Telegram process-ownership paths at a temp
    dir so no test can read or act on the real ``~/.claw/telegram.pid`` or the
    real ``~/.claw/telegram-poll-*.lock``:
      - ``TelegramTransport._PID_FILE`` (class attribute), and
      - ``claw_v2.telegram._DEFAULT_POLLING_LOCK_DIR`` (module constant read by
        ``_polling_lock_path`` when ``start()`` calls ``acquire_polling_lock``
        without an explicit ``base_dir``).

    It is deliberately narrow: ONLY these two Telegram ownership paths. No HOME
    rewrite, no runtime-config change, no production behavior change. Tests that
    set their own pid/lock paths (via ``patch.object``/``base_dir=``) are
    unaffected — their patch nests over this default and restores to it.
    """
    import claw_v2.telegram as telegram_mod

    original_pid_file = telegram_mod.TelegramTransport._PID_FILE
    original_lock_dir = telegram_mod._DEFAULT_POLLING_LOCK_DIR
    with tempfile.TemporaryDirectory(prefix="claw-test-telegram-isolation-") as tmpdir:
        tmp = Path(tmpdir)
        telegram_mod.TelegramTransport._PID_FILE = tmp / "telegram.pid"
        telegram_mod._DEFAULT_POLLING_LOCK_DIR = tmp
        try:
            yield tmp
        finally:
            telegram_mod.TelegramTransport._PID_FILE = original_pid_file
            telegram_mod._DEFAULT_POLLING_LOCK_DIR = original_lock_dir


# ---------------------------------------------------------------------------
# Foreign-process kill guard
#
# Incidente 2026-06-17: `pytest tests/` restarted the live production daemon.
# The Telegram pidfile path was one proven cause (isolated above), but the full
# suite restarted prod once more through a second, unpinned path. This guard is
# the class-wide safety net + culprit finder requested for that follow-up:
#
#   * It blocks any test from sending a *terminating* signal (SIGTERM/SIGINT/
#     SIGHUP/SIGKILL/SIGQUIT) to a *live foreign* process — one that is NOT the
#     pytest process or a child/grandchild spawned under it. "Foreign" is by
#     process ancestry, NOT by Unix user (the prod daemon runs as the same
#     user, so UID is not sufficient).
#   * Non-mutating probes (signal 0) are always allowed.
#   * Signals to a target that is already dead are allowed (the real call just
#     raises ProcessLookupError — harmless, and a dead pid can't be the live
#     daemon), which avoids false positives on cleanup of already-exited
#     children.
#   * Process-group / broadcast targets (os.kill pid<=0, os.killpg) for
#     terminating signals are unsafe by default and only allowed when the group
#     leader is a live descendant of the pytest tree.
#
# On a blocked attempt it records the offender and raises loudly so the
# offending test fails and is identified.
# ---------------------------------------------------------------------------

_GUARD_TERMINATING_SIGNALS = frozenset(
    int(sig)
    for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGKILL", "SIGQUIT")
    if (sig := getattr(signal, name, None)) is not None
)

# Captured at import, BEFORE any test patches os.kill / subprocess.run, so the
# guard never routes through a test's mock.
_GUARD_REAL_KILL = os.kill
_GUARD_REAL_KILLPG = getattr(os, "killpg", None)
_GUARD_REAL_RUN = subprocess.run

# Foreign-kill attempts recorded for an end-of-session summary (in case a SUT
# swallows the raised AssertionError).
_GUARD_VIOLATIONS: list[str] = []


class ForeignProcessKillBlocked(AssertionError):
    """A test tried to send a terminating signal to a live foreign process."""


def _guard_current_test() -> str:
    return os.environ.get("PYTEST_CURRENT_TEST", "<unknown test>")


def _guard_proc_alive(pid: int) -> bool:
    try:
        _GUARD_REAL_KILL(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _guard_ppid_of(pid: int) -> int | None:
    # No cross-call PPID cache on purpose: this is a safety guard, and a stale
    # cached PPID after PID reuse could misclassify a live foreign process as a
    # pytest descendant and ALLOW a kill of the production daemon. Terminating-
    # kill attempts in the suite are rare, so the per-call `ps` cost is fine.
    try:
        completed = _GUARD_REAL_RUN(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    raw = (completed.stdout or "").strip()
    if not raw:
        return None
    try:
        return int(raw.split()[0])
    except (ValueError, IndexError):
        return None


def _guard_is_descendant_of_pytest(pid: int) -> bool:
    me = os.getpid()
    if pid == me:
        return True
    seen: set[int] = set()
    cursor = pid
    for _ in range(64):
        parent = _guard_ppid_of(cursor)
        if parent is None or parent in (0, 1) or parent in seen:
            return False
        if parent == me:
            return True
        seen.add(parent)
        cursor = parent
    return False


def _guard_proc_cmd(pid: int) -> str:
    try:
        completed = _GUARD_REAL_RUN(
            ["ps", "-o", "command=", "-p", str(abs(pid))],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return (completed.stdout or "").strip()[:200] or "<no command>"
    except Exception:
        return "<unavailable>"


def _guard_block(sig_int: int, target: int, reason: str) -> None:
    try:
        sig_name = signal.Signals(sig_int).name
    except ValueError:
        sig_name = str(sig_int)
    cmd = _guard_proc_cmd(target) if target not in (0, -1) else "<process group / broadcast>"
    test_id = _guard_current_test()
    message = (
        "[foreign-kill-guard] BLOCKED a terminating signal to a live foreign process.\n"
        f"  signal     : {sig_name} ({sig_int})\n"
        f"  target     : {target} ({'process group' if target <= 0 else 'pid'})\n"
        f"  target cmd : {cmd}\n"
        f"  test       : {test_id}\n"
        f"  reason     : {reason}\n"
        "A test attempted to terminate a process outside the pytest tree. This is the "
        "class of defect that restarted the production daemon. Isolate this test "
        "(mock os.kill, use a test-owned child process, or patch the relevant pidfile)."
    )
    _GUARD_VIOLATIONS.append(message)
    # Surface immediately even if the caller swallows the exception.
    print("\n" + message, file=sys.stderr, flush=True)
    raise ForeignProcessKillBlocked(message)


def _guard_kill(pid: int, sig: int) -> None:
    sig_int = int(sig)
    if sig_int == 0 or sig_int not in _GUARD_TERMINATING_SIGNALS:
        return _GUARD_REAL_KILL(pid, sig)
    if pid <= 0:
        leader = abs(pid)
        if pid in (0, -1):
            _guard_block(sig_int, pid, "process-group/broadcast target (pid<=0)")
        if not _guard_proc_alive(leader):
            return _GUARD_REAL_KILL(pid, sig)
        if _guard_is_descendant_of_pytest(leader):
            return _GUARD_REAL_KILL(pid, sig)
        _guard_block(sig_int, pid, "live foreign process group")
    if not _guard_proc_alive(pid):
        return _GUARD_REAL_KILL(pid, sig)  # dead -> real ProcessLookupError, harmless
    if _guard_is_descendant_of_pytest(pid):
        return _GUARD_REAL_KILL(pid, sig)
    if not _guard_proc_alive(pid):
        return _GUARD_REAL_KILL(pid, sig)  # died during the ancestry check -> harmless
    _guard_block(sig_int, pid, "live foreign pid (outside pytest process tree)")


def _guard_killpg(pgid: int, sig: int) -> None:
    sig_int = int(sig)
    if sig_int == 0 or sig_int not in _GUARD_TERMINATING_SIGNALS:
        return _GUARD_REAL_KILLPG(pgid, sig)
    leader = abs(pgid)
    if leader == 0:
        _guard_block(sig_int, -leader, "own/broadcast process group (pgid 0)")
    # Dead group leader -> allow the real killpg (harmless ProcessLookupError on
    # an empty group). A *foreign* group whose leader has exited but whose
    # members linger is a known, accepted blind spot: once a leader dies its
    # children reparent to init, so ancestry can no longer prove ownership either
    # way, and blocking here would break legitimate cleanup of a test's own child
    # group (subprocess_runner SIGKILLs a group after SIGTERM). This guard targets
    # the observed failure mode (terminating a *live* foreign process / live-
    # leader group); it is a test safety net, not a security boundary.
    if not _guard_proc_alive(leader):
        return _GUARD_REAL_KILLPG(pgid, sig)
    if _guard_is_descendant_of_pytest(leader):
        return _GUARD_REAL_KILLPG(pgid, sig)
    if not _guard_proc_alive(leader):
        return _GUARD_REAL_KILLPG(pgid, sig)  # died during the ancestry check
    _guard_block(sig_int, -leader, "live foreign process group (killpg)")


@pytest.fixture(autouse=True, scope="session")
def _block_foreign_process_kills():
    os.kill = _guard_kill
    if _GUARD_REAL_KILLPG is not None:
        os.killpg = _guard_killpg
    try:
        yield
    finally:
        os.kill = _GUARD_REAL_KILL
        if _GUARD_REAL_KILLPG is not None:
            os.killpg = _GUARD_REAL_KILLPG
        if _GUARD_VIOLATIONS:
            print(
                f"\n[foreign-kill-guard] {len(_GUARD_VIOLATIONS)} foreign-kill attempt(s) "
                "were blocked during this session:",
                file=sys.stderr,
                flush=True,
            )
            for entry in _GUARD_VIOLATIONS:
                print(entry, file=sys.stderr, flush=True)
