"""Daemon liveness sink (F0.3).

The daemon's high-frequency liveness signal lives in a small, atomically
overwritten JSON file rather than as ``daemon_heartbeat`` / ``daemon_tick``
rows flooding ``observe_stream``. The authoritative writer is the scheduled
lifecycle heartbeat (``claw_v2/lifecycle.py``); the sole reader is the health
diagnostics path (``claw_v2/diagnostics.py``). Keeping the path constant
(``liveness_sink_path``) in both places is enforced by the
``test_liveness_signal_has_a_consumer`` architecture tripwire.

The write is overwrite-style (single current record), durable, and crash-safe:
a reader never observes a half-written file. The pattern mirrors
``coordinator._atomic_write_text`` (temp dot-file → ``os.write`` → ``fsync`` →
``os.replace`` → best-effort parent-dir fsync); it is intentionally duplicated
rather than imported so this leaf module has no dependency on the coordinator.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

LIVENESS_SINK_FILENAME = "liveness.json"


def liveness_sink_path(data_dir: Path | str) -> Path:
    """Return the liveness sink path inside ``data_dir`` (the SQLite data dir)."""
    return Path(data_dir) / LIVENESS_SINK_FILENAME


def write_liveness(path: Path, payload: dict) -> None:
    """Atomically overwrite ``path`` with ``payload`` as JSON.

    Mirrors ``coordinator._atomic_write_text``: a unique dot-prefixed tmp file
    is written and fsync'd, then ``os.replace``'d over the target so readers
    only ever see the old or the new complete file. The parent-directory fsync
    that makes the rename itself durable is best-effort — a failure there must
    not turn a successful, in-place write into a spurious error.
    """
    data = json.dumps(payload).encode("utf-8")
    tmp = path.parent / f".{path.name}.{secrets.token_hex(4)}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def read_liveness(path: Path) -> dict | None:
    """Return the parsed liveness record, or ``None`` if absent/unreadable.

    Returns ``None`` on a missing file, an OSError, invalid JSON, or a
    top-level value that is not a JSON object.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError (a ValueError) on a byte-corrupted sink must
        # degrade to None like a missing file — never escape into the
        # diagnostics/watchdog health path, which only guards sqlite errors.
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    return value
