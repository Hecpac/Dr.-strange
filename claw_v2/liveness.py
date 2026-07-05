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
from typing import Any

LIVENESS_SINK_FILENAME = "liveness.json"
RUNTIME_HEALTH_FIELD = "runtime_health"
SPILL_PENDING_COUNT_MAX_LINES = 10_000


def liveness_sink_path(data_dir: Path | str) -> Path:
    """Return the liveness sink path inside ``data_dir`` (the SQLite data dir)."""
    return Path(data_dir) / LIVENESS_SINK_FILENAME


def spill_pending_summary(
    db_path: Path | str,
    *,
    max_lines: int = SPILL_PENDING_COUNT_MAX_LINES,
) -> dict[str, Any]:
    """Count physical pending spill records next to ``db_path``.

    Malformed JSONL rows are still pending durable recovery work, so this count
    intentionally counts non-blank physical lines instead of parsing records.
    The scan is bounded; ``spill_pending_limited`` marks counts truncated at the
    line budget.
    """
    spill_path = Path(db_path).with_suffix(".spill.jsonl")
    line_limit = max(0, int(max_lines))
    pending_count = 0
    scanned_lines = 0
    limited = False
    try:
        with spill_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if scanned_lines >= line_limit:
                    limited = True
                    break
                scanned_lines += 1
                if line.strip():
                    pending_count += 1
    except FileNotFoundError:
        return {
            "spill_path": str(spill_path),
            "spill_pending_count": 0,
            "spill_pending_status": "missing",
            "spill_pending_limited": False,
            "spill_pending_limit": line_limit,
            "spill_lines_scanned": 0,
        }
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "spill_path": str(spill_path),
            "spill_pending_count": None,
            "spill_pending_status": "unreadable",
            "spill_pending_limited": False,
            "spill_pending_limit": line_limit,
            "spill_lines_scanned": scanned_lines,
            "error_type": type(exc).__name__,
        }
    return {
        "spill_path": str(spill_path),
        "spill_pending_count": pending_count,
        "spill_pending_status": "ok",
        "spill_pending_limited": limited,
        "spill_pending_limit": line_limit,
        "spill_lines_scanned": scanned_lines,
    }


def runtime_db_degraded_state(
    *,
    runtime_db: Any | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Return a serializable RuntimeDb degraded-state snapshot without probing."""
    reason = None
    if runtime_db is not None:
        reason = getattr(runtime_db, "degraded_reason", None)
    elif db_path is not None:
        from claw_v2.sqlite_runtime import runtime_db_degraded_reason

        reason = runtime_db_degraded_reason(db_path)
    if reason is None:
        return {"degraded": False, "reason_code": None, "reason": None}
    if hasattr(reason, "to_dict"):
        reason_dict = reason.to_dict()
    else:
        reason_dict = {"message": str(reason)}
    return {
        "degraded": True,
        "reason_code": reason_dict.get("reason_code"),
        "reason": reason_dict,
    }


def runtime_health_snapshot(
    *,
    db_path: Path | str,
    db_write_probe_status: str | None,
    runtime_db: Any | None = None,
) -> dict[str, Any]:
    """Compact runtime health surface consumed by liveness diagnostics."""
    spill = spill_pending_summary(db_path)
    degraded_state = runtime_db_degraded_state(runtime_db=runtime_db, db_path=db_path)
    return {
        "spill_pending_count": spill["spill_pending_count"],
        "spill_pending_status": spill["spill_pending_status"],
        "spill_pending_limited": spill["spill_pending_limited"],
        "spill_pending_limit": spill["spill_pending_limit"],
        "spill_lines_scanned": spill["spill_lines_scanned"],
        "spill_path": spill["spill_path"],
        "db_write_probe_status": db_write_probe_status,
        "runtime_db_degraded": bool(degraded_state["degraded"]),
        "runtime_db_degraded_state": degraded_state,
    }


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
