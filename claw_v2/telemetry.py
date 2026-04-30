from __future__ import annotations

import fcntl
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claw_v2.redaction import redact_sensitive

logger = logging.getLogger(__name__)


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
    line = json.dumps(redacted, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"

    if len(line.encode("utf-8")) > 1_048_576:
        raise ValueError(f"jsonl line exceeds 1MB cap: {len(line)} bytes for {target.name}")

    lock_path = target.with_suffix(target.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


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
