#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

from claw_v2.sqlite_runtime import RuntimeDatabaseError, check_runtime_sqlite_health


def create_verified_backup(
    db_path: Path,
    backup_dir: Path,
    *,
    now: float | None = None,
) -> Path | None:
    """Health-check a runtime DB and create a verified SQLite backup.

    Missing or empty databases are valid first-boot states; in those cases there
    is nothing useful to back up.
    """
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    check_runtime_sqlite_health(db_path, thorough=True)
    if not db_path.exists() or db_path.stat().st_size == 0:
        return None

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(time.time() if now is None else now))
    backup_path = _unique_backup_path(backup_dir / f"{db_path.stem}-{stamp}.db")

    source = sqlite3.connect(
        f"file:{db_path}?mode=ro",
        uri=True,
        timeout=30.0,
    )
    target = sqlite3.connect(backup_path, timeout=30.0)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()

    check_runtime_sqlite_health(backup_path, thorough=True)
    return backup_path


def _unique_backup_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeDatabaseError(f"could not allocate unique backup path under {path.parent}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify and back up the Claw runtime DB.")
    parser.add_argument("--db", default="data/claw.db", type=Path)
    parser.add_argument("--backup-dir", default="data/backups/restart", type=Path)
    args = parser.parse_args(argv)

    try:
        backup_path = create_verified_backup(args.db, args.backup_dir)
    except (RuntimeDatabaseError, sqlite3.DatabaseError, OSError) as exc:
        print(f"ERROR: runtime DB preflight failed: {exc}", file=sys.stderr)
        return 1
    if backup_path is None:
        print(f"Runtime DB preflight OK: no backup needed for {args.db}")
    else:
        print(f"Runtime DB preflight OK: verified backup {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
