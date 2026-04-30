from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claw_v2.redaction import redact_sensitive

logger = logging.getLogger(__name__)

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_LOCK = threading.Lock()


def generate_id(prefix: str) -> str:
    cleaned = "".join(char for char in prefix.lower() if char.isalnum())
    if not cleaned:
        raise ValueError("id prefix is required")
    return f"{cleaned}_{uuid.uuid4().hex}"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def append_jsonl(path: Path | str, record: dict[str, Any]) -> None:
    target = Path(path).expanduser()
    redacted = redact_sensitive(dict(record), limit=4000)
    line = json.dumps(redacted, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    lock = _lock_for(target)
    with lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")


def read_jsonl(path: Path | str) -> list[dict[str, Any]]:
    target = Path(path).expanduser()
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Skipping corrupt JSONL line %s in %s", line_number, target)
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
            else:
                logger.warning("Skipping non-object JSONL line %s in %s", line_number, target)
    return rows


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_LOCK:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock

