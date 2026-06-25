from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from claw_v2 import f2_primary_compat_preflight as preflight
from claw_v2.f2_durability_schema import (
    F2_DURABILITY_SCHEMA_VERSION,
    F2_DURABILITY_TABLES,
)
from claw_v2.f2_durability_store import F2DurabilityStore
from claw_v2.sqlite_runtime import RuntimeDb

_EXPECTED_UNIQUE_INDEXES = {
    "ux_phase_checkpoints_task_run_phase_version",
    "ux_external_effect_records_idempotency_key",
    "ux_phase_checkpoint_writes_order",
    "ux_phase_checkpoint_writes_key",
    "ux_phase_recovery_cursors_task_run",
}


class ExpectedSchemaTests(unittest.TestCase):
    def test_expected_schema_has_f2_tables_and_unique_indexes(self) -> None:
        expected = preflight.expected_f2_schema()
        self.assertEqual(expected.schema_version, F2_DURABILITY_SCHEMA_VERSION)
        # All 4 F2 tables present with non-empty column lists.
        self.assertEqual(set(expected.tables), set(F2_DURABILITY_TABLES))
        for table, cols in expected.tables.items():
            self.assertTrue(cols, f"no columns introspected for {table}")
        # The 5 F2 unique indexes are a subset of what was introspected.
        self.assertTrue(
            _EXPECTED_UNIQUE_INDEXES.issubset(set(expected.unique_indexes)),
            f"missing F2 unique indexes: "
            f"{_EXPECTED_UNIQUE_INDEXES - set(expected.unique_indexes)}",
        )
        idem = expected.unique_indexes["ux_external_effect_records_idempotency_key"]
        self.assertTrue(idem.unique)
        self.assertEqual(set(idem.columns), {"idempotency_key"})


class PathCheckTests(unittest.TestCase):
    def _open_primary(self, tmpdir: str, *, build: bool = True) -> sqlite3.Connection:
        """Build a temp 'primary' with the F2 schema, keep a writer open (so the
        read-only open sees the WAL), and return a read-only connection."""
        path = Path(tmpdir) / "claw.db"
        wdb = RuntimeDb(path)
        if build:
            F2DurabilityStore(wdb)
        self.addCleanup(wdb.close)
        self._writers = getattr(self, "_writers", [])
        self._writers.append(wdb)
        conn = preflight._open_readonly(path)
        self.addCleanup(conn.close)
        return conn, wdb

    def test_schema_check_passes_on_matching_db(self) -> None:
        expected = preflight.expected_f2_schema()
        with tempfile.TemporaryDirectory() as tmpdir:
            conn, _ = self._open_primary(tmpdir)
            result = preflight._check_schema(conn, expected)
        self.assertEqual(result.status, preflight.PASS)

    def test_schema_check_fails_on_missing_table(self) -> None:
        expected = preflight.expected_f2_schema()
        with tempfile.TemporaryDirectory() as tmpdir:
            conn, wdb = self._open_primary(tmpdir)
            with wdb.transaction() as cur:
                cur.execute("DROP TABLE phase_recovery_cursors")
            result = preflight._check_schema(conn, expected)
        self.assertEqual(result.status, preflight.FAIL)
        self.assertTrue(any("missing_table:phase_recovery_cursors" in r for r in result.reasons))

    def test_index_check_fails_on_missing_unique_index(self) -> None:
        expected = preflight.expected_f2_schema()
        with tempfile.TemporaryDirectory() as tmpdir:
            conn, wdb = self._open_primary(tmpdir)
            with wdb.transaction() as cur:
                cur.execute("DROP INDEX ux_external_effect_records_idempotency_key")
            result = preflight._check_indexes(conn, expected)
        self.assertEqual(result.status, preflight.FAIL)
        self.assertTrue(
            any("missing_unique_index:ux_external_effect_records_idempotency_key" in r
                for r in result.reasons)
        )

    def test_counts_check_empty_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            conn, _ = self._open_primary(tmpdir)
            result = preflight._check_counts(conn)
        self.assertEqual(result.status, preflight.PASS)
        self.assertEqual(result.details["non_empty_f2_tables"], [])
        self.assertEqual(
            set(result.details["f2_table_counts"]), set(F2_DURABILITY_TABLES)
        )

    def test_counts_check_nonempty_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            conn, wdb = self._open_primary(tmpdir)
            store = F2DurabilityStore(wdb)
            store.append_checkpoint_write(
                task_id="compat-probe", run_id="compat-probe", phase="implementation",
                write_kind="phase_started", payload={"event": "probe"},
            )
            result = preflight._check_counts(conn)
        self.assertEqual(result.status, preflight.PASS)
        self.assertIn("phase_checkpoint_writes", result.details["non_empty_f2_tables"])

    def test_integrity_check_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            conn, _ = self._open_primary(tmpdir)
            result = preflight._check_integrity(conn)
        self.assertEqual(result.status, preflight.PASS)


_REQUIRED_JSON_FIELDS = (
    "overall_status",
    "db_path_checked",
    "opened_read_only",
    "immutable_mode_used",
    "primary_db_touched",
    "schema_version_expected",
    "schema_version_found",
    "schema_path",
    "index_path",
    "counts_path",
    "integrity_path",
    "integrity_required",
    "f2_table_counts",
    "non_empty_f2_tables",
    "reasons",
    "checks",
    "recommendation",
    "does_not_prove",
)


class RunReportTests(unittest.TestCase):
    def _primary_with_writer(self, tmpdir: str):
        path = Path(tmpdir) / "claw.db"
        wdb = RuntimeDb(path)
        F2DurabilityStore(wdb)
        self.addCleanup(wdb.close)
        return path, wdb

    def test_smoke_temp_db_passes(self) -> None:
        report = preflight.run_primary_compat_preflight()
        self.assertEqual(report["overall_status"], preflight.PASS)
        self.assertEqual(report["recommendation"], preflight.READY)
        self.assertFalse(report["primary_db_touched"])
        self.assertTrue(report["opened_read_only"])
        self.assertFalse(report["immutable_mode_used"])
        self.assertTrue(report["integrity_required"])

    def test_json_output_contains_required_fields(self) -> None:
        report = preflight.run_primary_compat_preflight()
        for key in _REQUIRED_JSON_FIELDS:
            self.assertIn(key, report)
        self.assertEqual(report["does_not_prove"], preflight._DOES_NOT_PROVE)

    def test_matching_primary_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path, _ = self._primary_with_writer(tmpdir)
            report = preflight.run_primary_compat_preflight(db_path=str(path))
        self.assertEqual(report["overall_status"], preflight.PASS)
        self.assertEqual(report["recommendation"], preflight.READY)
        self.assertFalse(report["primary_db_touched"])

    def test_missing_table_needs_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path, wdb = self._primary_with_writer(tmpdir)
            with wdb.transaction() as cur:
                cur.execute("DROP TABLE phase_recovery_cursors")
            report = preflight.run_primary_compat_preflight(db_path=str(path))
        self.assertEqual(report["overall_status"], preflight.FAIL)
        self.assertEqual(report["recommendation"], preflight.NEEDS_REPAIR)

    def test_missing_unique_index_needs_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path, wdb = self._primary_with_writer(tmpdir)
            with wdb.transaction() as cur:
                cur.execute("DROP INDEX ux_phase_recovery_cursors_task_run")
            report = preflight.run_primary_compat_preflight(db_path=str(path))
        self.assertEqual(report["overall_status"], preflight.FAIL)
        self.assertEqual(report["recommendation"], preflight.NEEDS_REPAIR)

    def test_subset_extra_objects_still_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path, wdb = self._primary_with_writer(tmpdir)
            with wdb.transaction() as cur:
                cur.execute("ALTER TABLE phase_checkpoints ADD COLUMN extra_col TEXT")
                cur.execute("CREATE TABLE unrelated_extra (id TEXT PRIMARY KEY)")
            report = preflight.run_primary_compat_preflight(db_path=str(path))
        self.assertEqual(report["overall_status"], preflight.PASS)

    def test_open_failure_is_blocked(self) -> None:
        report = preflight.run_primary_compat_preflight(db_path="/nonexistent/dir/claw.db")
        self.assertEqual(report["overall_status"], preflight.FAIL)
        self.assertEqual(report["recommendation"], preflight.BLOCKED)
        self.assertFalse(report["opened_read_only"])
        self.assertFalse(report["primary_db_touched"])

    def test_read_only_enforcement_write_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path, _ = self._primary_with_writer(tmpdir)
            conn = preflight._open_readonly(path)
            self.addCleanup(conn.close)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE should_fail (x TEXT)")
            query_only = conn.execute("PRAGMA query_only").fetchone()[0]
        self.assertEqual(int(query_only), 1)


if __name__ == "__main__":
    unittest.main()
