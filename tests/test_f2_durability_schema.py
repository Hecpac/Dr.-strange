from __future__ import annotations

import inspect
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import claw_v2.f2_durability_schema as f2_schema
from claw_v2.f2_durability_schema import (
    F2_DURABILITY_INDEXES,
    F2_DURABILITY_SCHEMA_VERSION,
    F2_DURABILITY_TABLES,
    ensure_f2_durability_schema,
    validate_f2_schema_version,
)
from claw_v2.sqlite_runtime import RuntimeDb, _registry_key, _WAL_HEAL_REGISTRY


class F2DurabilitySchemaTests(unittest.TestCase):
    def _runtime_db(self, tmpdir: str) -> RuntimeDb:
        db = RuntimeDb(Path(tmpdir) / "claw.db")
        self.addCleanup(db.close)
        return db

    def test_migration_creates_all_f2_tables_on_temp_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)

            ensure_f2_durability_schema(db)

            with db.cursor() as cur:
                tables = {
                    row["name"]
                    for row in cur.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                self.assertTrue(set(F2_DURABILITY_TABLES).issubset(tables))

                for table in F2_DURABILITY_TABLES:
                    columns = {row["name"] for row in cur.execute(f"PRAGMA table_info({table})")}
                    self.assertIn("schema_version", columns)

                writes_fk = {
                    (row["from"], row["table"], row["to"])
                    for row in cur.execute("PRAGMA foreign_key_list(phase_checkpoint_writes)")
                }
                self.assertIn(
                    (
                        "external_effect_id",
                        "external_effect_records",
                        "external_effect_id",
                    ),
                    writes_fk,
                )

                cursors_fk = {
                    (row["from"], row["table"], row["to"])
                    for row in cur.execute("PRAGMA foreign_key_list(phase_recovery_cursors)")
                }
                self.assertIn(
                    ("last_checkpoint_id", "phase_checkpoints", "checkpoint_id"),
                    cursors_fk,
                )
                self.assertIn(
                    (
                        "external_effect_id",
                        "external_effect_records",
                        "external_effect_id",
                    ),
                    cursors_fk,
                )

    def test_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)

            ensure_f2_durability_schema(db)
            ensure_f2_durability_schema(db)

            with db.cursor() as cur:
                tables = [
                    row["name"]
                    for row in cur.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' "
                        "AND name IN ({})".format(",".join("?" for _ in F2_DURABILITY_TABLES)),
                        F2_DURABILITY_TABLES,
                    ).fetchall()
                ]
                self.assertEqual(set(tables), set(F2_DURABILITY_TABLES))

    def test_expected_indexes_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)

            ensure_f2_durability_schema(db)

            with db.cursor() as cur:
                indexes = {
                    row["name"]
                    for row in cur.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    ).fetchall()
                }
            self.assertTrue(set(F2_DURABILITY_INDEXES).issubset(indexes))

    def test_migration_uses_runtimedb_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)

            with patch.object(db, "transaction", wraps=db.transaction) as transaction:
                ensure_f2_durability_schema(db)

            transaction.assert_called_once()

    def test_schema_module_does_not_open_independent_sqlite_connections(self) -> None:
        source = inspect.getsource(f2_schema)

        self.assertNotIn("sqlite3.connect", source)
        self.assertNotIn("connect_runtime_sqlite", source)
        self.assertIn("runtime_db.transaction()", source)

    def test_migration_does_not_register_wal_heal_or_reopen_runtimedb(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            old_connection_id = db.current_connection_id()

            ensure_f2_durability_schema(db)
            ensure_f2_durability_schema(db)

            self.assertEqual(db.current_connection_id(), old_connection_id)
            handles = _WAL_HEAL_REGISTRY.get(_registry_key(db.db_path), [])
            self.assertEqual([handle for handle in handles if handle.alive], [])

    def test_insert_select_smoke_for_each_table_uses_test_data_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            ensure_f2_durability_schema(db)

            with db.transaction() as cur:
                cur.execute(
                    """
                    INSERT INTO phase_checkpoints (
                        checkpoint_id, task_id, run_id, job_id, session_id, phase,
                        phase_version, status, schema_version, last_write_order,
                        payload_json, payload_sha256, orchestration_run_id,
                        orchestration_checkpoint_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "checkpoint-test",
                        "task-test",
                        "run-test",
                        "job-test",
                        "session-test",
                        "research",
                        1,
                        "started",
                        F2_DURABILITY_SCHEMA_VERSION,
                        1,
                        '{"phase":"research"}',
                        "sha256:checkpoint",
                        "orch-run-test",
                        "orch-checkpoint-test",
                        "2026-06-24T00:00:00Z",
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO external_effect_records (
                        external_effect_id, idempotency_key, task_id, run_id, job_id,
                        phase, effect_kind, target, content_hash, request_json,
                        request_sha256, status, attempt_count, verifier_kind,
                        verification_json, result_json, result_sha256, error,
                        schema_version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "effect-test",
                        "idem-test",
                        "task-test",
                        "run-test",
                        "job-test",
                        "research",
                        "test_effect",
                        "test-target",
                        "sha256:content",
                        '{"request":true}',
                        "sha256:request",
                        "intent_recorded",
                        0,
                        "test_verifier",
                        None,
                        None,
                        None,
                        None,
                        F2_DURABILITY_SCHEMA_VERSION,
                        "2026-06-24T00:00:00Z",
                        "2026-06-24T00:00:00Z",
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO phase_checkpoint_writes (
                        write_id, task_id, run_id, job_id, phase, write_order,
                        write_kind, write_key, schema_version, payload_json,
                        payload_sha256, external_effect_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "write-test",
                        "task-test",
                        "run-test",
                        "job-test",
                        "research",
                        1,
                        "external_effect_intent",
                        "intent:test",
                        F2_DURABILITY_SCHEMA_VERSION,
                        '{"write":true}',
                        "sha256:write",
                        "effect-test",
                        "2026-06-24T00:00:00Z",
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO phase_recovery_cursors (
                        recovery_cursor_id, task_id, run_id, job_id, session_id,
                        phase, cursor_status, last_checkpoint_id, last_write_order,
                        external_effect_id, resume_payload_json, schema_version,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "cursor-test",
                        "task-test",
                        "run-test",
                        "job-test",
                        "session-test",
                        "research",
                        "effect_verification_required",
                        "checkpoint-test",
                        1,
                        "effect-test",
                        '{"resume":true}',
                        F2_DURABILITY_SCHEMA_VERSION,
                        "2026-06-24T00:00:00Z",
                        "2026-06-24T00:00:00Z",
                    ),
                )

            with db.cursor() as cur:
                for table in F2_DURABILITY_TABLES:
                    self.assertEqual(
                        cur.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"],
                        1,
                    )
                row = cur.execute(
                    "SELECT schema_version, cursor_status FROM phase_recovery_cursors"
                ).fetchone()
                self.assertEqual(row["schema_version"], F2_DURABILITY_SCHEMA_VERSION)
                self.assertEqual(row["cursor_status"], "effect_verification_required")

    def test_external_effect_idempotency_key_is_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            ensure_f2_durability_schema(db)

            def insert_effect(effect_id: str) -> None:
                with db.transaction() as cur:
                    cur.execute(
                        """
                        INSERT INTO external_effect_records (
                            external_effect_id, idempotency_key, task_id, run_id,
                            phase, effect_kind, target, content_hash, request_json,
                            request_sha256, status, schema_version, created_at,
                            updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            effect_id,
                            "same-idempotency-key",
                            "task-test",
                            "run-test",
                            "research",
                            "test_effect",
                            "test-target",
                            "sha256:content",
                            "{}",
                            "sha256:request",
                            "intent_recorded",
                            F2_DURABILITY_SCHEMA_VERSION,
                            "2026-06-24T00:00:00Z",
                            "2026-06-24T00:00:00Z",
                        ),
                    )

            insert_effect("effect-1")
            with self.assertRaises(sqlite3.IntegrityError):
                insert_effect("effect-2")

    def test_write_key_uniqueness_is_partial_for_non_null_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            ensure_f2_durability_schema(db)

            def insert_write(write_id: str, order: int, write_key: str | None) -> None:
                with db.transaction() as cur:
                    cur.execute(
                        """
                        INSERT INTO phase_checkpoint_writes (
                            write_id, task_id, run_id, phase, write_order,
                            write_kind, write_key, schema_version, payload_json,
                            payload_sha256, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            write_id,
                            "task-test",
                            "run-test",
                            "research",
                            order,
                            "artifact_recorded",
                            write_key,
                            F2_DURABILITY_SCHEMA_VERSION,
                            "{}",
                            "sha256:write",
                            "2026-06-24T00:00:00Z",
                        ),
                    )

            insert_write("write-1", 1, "artifact:test")
            with self.assertRaises(sqlite3.IntegrityError):
                insert_write("write-2", 2, "artifact:test")

            insert_write("write-3", 3, None)
            insert_write("write-4", 4, None)

    def test_schema_version_validation_accepts_only_supported_version(self) -> None:
        validate_f2_schema_version(F2_DURABILITY_SCHEMA_VERSION)

        with self.assertRaises(ValueError):
            validate_f2_schema_version(F2_DURABILITY_SCHEMA_VERSION + 1)


if __name__ == "__main__":
    unittest.main()
