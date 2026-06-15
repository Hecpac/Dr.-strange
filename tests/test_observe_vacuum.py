"""F0.2b — off-tick VACUUM reclaims space.

``ObserveStream.maintenance_vacuum`` rewrites the database file to reclaim
pages freed by prune. It is blocking + needs ~2x free disk, so it must only
run off-tick (enforced by test_vacuum_only_runs_off_tick); here we only pin
that it actually shrinks the file.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.observe import ObserveStream


class ObserveMaintenanceVacuumTests(unittest.TestCase):
    def test_observe_maintenance_vacuum_reclaims_space(self) -> None:
        db_path = Path(tempfile.mkdtemp()) / "observe.db"
        observe = ObserveStream(db_path)

        # Seed many sizeable rows, then flush WAL into the main db file so its
        # on-disk size reflects the bloat.
        big = "x" * 2_000
        with observe._lock:
            for i in range(3_000):
                observe._conn.execute(
                    "INSERT INTO observe_stream (event_type, payload) VALUES ('e', ?)",
                    (f'{{"i":{i},"blob":"{big}"}}',),
                )
            observe._conn.commit()
            observe._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        # Delete almost everything, then checkpoint so the freed pages are in
        # the main db file (size stays large until VACUUM reclaims them).
        observe.prune(retention_days=30, max_rows=10_000, max_total_rows=10)
        with observe._lock:
            observe._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        size_before = db_path.stat().st_size
        observe.maintenance_vacuum()
        size_after = db_path.stat().st_size

        self.assertLess(size_after, size_before)
        # The data is intact (cap kept the 10 highest ids).
        count = observe._conn.execute("SELECT COUNT(*) FROM observe_stream").fetchone()[0]
        self.assertEqual(count, 10)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
