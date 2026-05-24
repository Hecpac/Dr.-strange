"""P0-A: output-safety helpers for behavior_audit extraction.

On 2026-05-23 the daemon's canonical extractor and an ad-hoc Claude Code
extractor ran concurrently against the same output directory; the
daemon's `behavior_cases_sample.jsonl` silently overwrote Claude Code's,
because both writers used the same hard-coded filename. This module
provides per-run identifiers, O_EXCL canonical writes, and a frontmatter
builder so concurrent runs each preserve their artifacts and every
output is self-describing.
"""

from __future__ import annotations

import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path


def generate_run_id() -> str:
    """Opaque, sortable run id: ``<epoch_seconds>-<8 hex chars>``.

    The epoch prefix keeps the lexicographic order matching creation order,
    which makes ``ls`` output naturally readable; the random suffix avoids
    collisions when two extractor processes start in the same second.
    """
    return f"{int(time.time())}-{secrets.token_hex(4)}"


def build_output_paths(out_dir: Path | str, run_id: str) -> dict[str, Path]:
    """Return canonical + run-suffixed output paths for the extractor.

    ``canonical_*`` files are only ever written exclusively (O_EXCL); if
    they already exist they are left alone. ``run_*`` files always
    contain the current run's artifacts and are unique per ``run_id``.
    """
    base = Path(out_dir)
    return {
        "canonical_jsonl": base / "behavior_cases_sample.jsonl",
        "canonical_md": base / "BEHAVIOR_AUDIT_REPORT.md",
        "run_jsonl": base / f"behavior_cases_sample_{run_id}.jsonl",
        "run_md": base / f"BEHAVIOR_AUDIT_REPORT_{run_id}.md",
    }


def write_exclusive(path: Path | str, content: str | bytes) -> bool:
    """Write ``content`` to ``path`` only if the file does not yet exist.

    Returns True when the write happened, False when the file already
    existed (another writer won the race). This is the OS-level guarantee
    the canonical outputs rely on so two concurrent extractor processes
    cannot clobber each other.
    """
    p = Path(path)
    data = content.encode("utf-8") if isinstance(content, str) else content
    try:
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return True


def build_frontmatter(
    *,
    run_id: str,
    generated_by: str,
    source: str,
    started_at: float,
    completed_at: float,
    canonical: bool,
    input_db: str | Path,
    sample_size: int,
) -> str:
    """Return a YAML-frontmatter block describing how the report was made.

    Sticks a self-describing header on every persisted markdown report so
    downstream readers (Hector, the bot's sanitizer, future audit tooling)
    can tell daemon-canonical runs from ad-hoc Claude Code runs and which
    input DB they used.
    """
    started = datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat()
    completed = datetime.fromtimestamp(completed_at, tz=timezone.utc).isoformat()
    return (
        "---\n"
        f"run_id: {run_id}\n"
        f"generated_by: {generated_by}\n"
        f"source: {source}\n"
        f"started_at: {started}\n"
        f"completed_at: {completed}\n"
        f"canonical: {'true' if canonical else 'false'}\n"
        f"input_db: {input_db}\n"
        f"sample_size: {sample_size}\n"
        "---\n\n"
    )
