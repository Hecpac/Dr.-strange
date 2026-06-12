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
