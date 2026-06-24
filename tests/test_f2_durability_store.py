from __future__ import annotations

import hashlib
import inspect
import json
import tempfile
import threading
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

import claw_v2.f2_durability_store as f2_store_module
from claw_v2.f2_durability_schema import F2_DURABILITY_TABLES
from claw_v2.f2_durability_store import (
    F2DurabilityStore,
    compute_external_effect_idempotency_key,
)
from claw_v2.sqlite_runtime import RuntimeDb


class F2DurabilityStoreTests(unittest.TestCase):
    def _runtime_db(self, tmpdir: str) -> RuntimeDb:
        db = RuntimeDb(Path(tmpdir) / "claw.db")
        self.addCleanup(db.close)
        return db

    def _store(self, tmpdir: str) -> tuple[F2DurabilityStore, RuntimeDb]:
        db = self._runtime_db(tmpdir)
        return F2DurabilityStore(db), db

    def test_store_initializes_schema_on_temp_runtimedb(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, db = self._store(tmpdir)

            with db.cursor() as cur:
                tables = {
                    row["name"]
                    for row in cur.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }

            self.assertIsInstance(store, F2DurabilityStore)
            self.assertTrue(set(F2_DURABILITY_TABLES).issubset(tables))

    def test_schema_init_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)

            F2DurabilityStore(db)
            F2DurabilityStore(db)

            with db.cursor() as cur:
                count = cur.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM sqlite_master
                    WHERE type = 'table'
                      AND name IN ({})
                    """.format(",".join("?" for _ in F2_DURABILITY_TABLES)),
                    F2_DURABILITY_TABLES,
                ).fetchone()["n"]
            self.assertEqual(count, len(F2_DURABILITY_TABLES))

    def test_repeated_store_construction_uses_schema_ready_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)

            with patch.object(
                f2_store_module,
                "ensure_f2_durability_schema",
                wraps=f2_store_module.ensure_f2_durability_schema,
            ) as ensure_schema:
                first = F2DurabilityStore(db)
                second = F2DurabilityStore(db)

                first.create_phase_checkpoint(
                    checkpoint_id="checkpoint-1",
                    task_id="task-1",
                    run_id="run-1",
                    phase="research",
                    phase_version=1,
                    status="started",
                    payload={},
                )
                second.list_phase_checkpoints(task_id="task-1")

            ensure_schema.assert_called_once_with(db)

    def test_create_and_read_phase_checkpoint_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, _db = self._store(tmpdir)

            created = store.create_phase_checkpoint(
                checkpoint_id="checkpoint-1",
                task_id="task-1",
                run_id="run-1",
                job_id="job-1",
                session_id="session-1",
                phase="research",
                phase_version=1,
                status="started",
                last_write_order=2,
                payload={"phase": "research", "items": [1, 2]},
                orchestration_run_id="orch-run-1",
                orchestration_checkpoint_id="orch-checkpoint-1",
                created_at="2026-06-24T00:00:00Z",
            )
            loaded = store.get_phase_checkpoint("checkpoint-1")

            self.assertEqual(loaded, created)
            self.assertEqual(loaded.payload, {"phase": "research", "items": [1, 2]})
            self.assertTrue(loaded.payload_sha256.startswith("sha256:"))
            self.assertIsNone(loaded.payload_error)

    def test_unicode_json_roundtrips_without_ascii_escaping(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, db = self._store(tmpdir)
            payload = {
                "accent": "caf\u00e9",
                "city": "\u6771\u4eac",
                "emoji": "\U0001f680",
            }

            created = store.create_phase_checkpoint(
                checkpoint_id="checkpoint-unicode",
                task_id="task-1",
                run_id="run-1",
                phase="research",
                phase_version=1,
                status="started",
                payload=payload,
            )

            with db.cursor() as cur:
                payload_json = cur.execute(
                    """
                    SELECT payload_json
                    FROM phase_checkpoints
                    WHERE checkpoint_id = ?
                    """,
                    (created.checkpoint_id,),
                ).fetchone()["payload_json"]

            self.assertEqual(created.payload, payload)
            self.assertIn(payload["accent"], payload_json)
            self.assertIn(payload["city"], payload_json)
            self.assertIn(payload["emoji"], payload_json)
            self.assertNotIn("\\u00e9", payload_json)
            self.assertNotIn("\\u6771", payload_json)
            self.assertNotIn("\\ud83d", payload_json)

    def test_list_phase_checkpoints_is_filtered_and_deterministically_ordered(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, _db = self._store(tmpdir)
            for version in (2, 1, 3):
                store.create_phase_checkpoint(
                    checkpoint_id=f"checkpoint-{version}",
                    task_id="task-1",
                    run_id="run-1",
                    phase="verification",
                    phase_version=version,
                    status="started" if version < 3 else "succeeded",
                    payload={"version": version},
                    created_at=f"2026-06-24T00:00:0{version}Z",
                )
            store.create_phase_checkpoint(
                checkpoint_id="checkpoint-other",
                task_id="task-2",
                run_id="run-2",
                phase="research",
                phase_version=1,
                status="started",
                payload={},
            )

            ordered = store.list_phase_checkpoints(
                task_id="task-1",
                run_id="run-1",
                phase="verification",
                order="phase_version_asc",
            )
            succeeded = store.list_phase_checkpoints(task_id="task-1", status="succeeded")

            self.assertEqual([row.phase_version for row in ordered], [1, 2, 3])
            self.assertEqual([row.checkpoint_id for row in succeeded], ["checkpoint-3"])

    def test_append_and_list_checkpoint_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, _db = self._store(tmpdir)

            first = store.append_checkpoint_write(
                write_id="write-1",
                task_id="task-1",
                run_id="run-1",
                phase="implementation",
                write_kind="phase_started",
                write_key="phase-started",
                payload={"ok": True},
                created_at="2026-06-24T00:00:01Z",
            )
            second = store.append_checkpoint_write(
                write_id="write-2",
                task_id="task-1",
                run_id="run-1",
                phase="implementation",
                write_kind="phase_return",
                payload={"result": "done"},
                created_at="2026-06-24T00:00:02Z",
            )

            writes = store.list_checkpoint_writes(
                task_id="task-1",
                run_id="run-1",
                phase="implementation",
            )

            self.assertEqual((first.write_order, second.write_order), (1, 2))
            self.assertEqual([row.write_id for row in writes], ["write-1", "write-2"])
            self.assertEqual(writes[1].payload, {"result": "done"})

    def test_record_external_effect_roundtrip_and_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, db = self._store(tmpdir)
            key = compute_external_effect_idempotency_key(
                task_id="task-1",
                run_id="run-1",
                phase="implementation",
                effect_kind="github_pr",
                target="Hecpac/repo#draft",
                content_hash="sha256:content",
            )

            first = store.record_external_effect(
                external_effect_id="effect-1",
                idempotency_key=key,
                task_id="task-1",
                run_id="run-1",
                phase="implementation",
                effect_kind="github_pr",
                target="Hecpac/repo#draft",
                content_hash="sha256:content",
                request={"title": "Draft PR"},
            )
            second = store.record_external_effect(
                external_effect_id="effect-2",
                idempotency_key=key,
                task_id="task-1",
                run_id="run-1",
                phase="implementation",
                effect_kind="github_pr",
                target="Hecpac/repo#draft",
                content_hash="sha256:content",
                request={"title": "Different duplicate request"},
            )
            by_key = store.get_external_effect_by_idempotency_key(key)
            listed = store.list_external_effects(
                task_id="task-1",
                run_id="run-1",
                phase="implementation",
                status="intent_recorded",
            )

            self.assertEqual(first.external_effect_id, "effect-1")
            self.assertEqual(second.external_effect_id, "effect-1")
            self.assertEqual(by_key.external_effect_id, "effect-1")
            self.assertEqual(by_key.request, {"title": "Draft PR"})
            self.assertEqual([row.external_effect_id for row in listed], ["effect-1"])
            self.assertEqual(second.request, first.request)
            with db.cursor() as cur:
                row = cur.execute(
                    """
                    SELECT COUNT(*) AS n, request_json
                    FROM external_effect_records
                    WHERE idempotency_key = ?
                    """,
                    (key,),
                ).fetchone()
            self.assertEqual(row["n"], 1)
            self.assertNotIn("Different duplicate request", row["request_json"])

    def test_external_effect_redacts_persisted_metadata_and_hashes_redacted_json(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, db = self._store(tmpdir)

            created = store.record_external_effect(
                external_effect_id="effect-secret",
                task_id="task-1",
                run_id="run-1",
                phase="implementation",
                effect_kind="telegram_send",
                target="chat:owner",
                request={
                    "api_key": "sk-requestsecret12345678901234567890",
                    "headers": {
                        "authorization": "Bearer requestsecret12345678901234567890",
                        "cookie": "sid=request-cookie-secret",
                    },
                    "items": [
                        {
                            "access_token": "list-secret-token",
                            "safe": "list-metadata",
                        }
                    ],
                    "telegram_bot_token": "123456789:abcdefghijklmnopqrstuvwxyzABCDE",
                    "safe": "metadata",
                },
                verification={
                    "access_token": "verify-secret-token",
                    "status": "pending",
                },
                result={
                    "cookie": "sid=result-cookie-secret",
                    "message_id": "42",
                },
                error="failed with Bearer errorsecret12345678901234567890",
            )

            with db.cursor() as cur:
                row = cur.execute(
                    """
                    SELECT request_json, request_sha256, verification_json,
                           result_json, result_sha256, error
                    FROM external_effect_records
                    WHERE external_effect_id = ?
                    """,
                    (created.external_effect_id,),
                ).fetchone()

            persisted = "\n".join(
                str(row[column] or "")
                for column in (
                    "request_json",
                    "verification_json",
                    "result_json",
                    "error",
                )
            )
            self.assertNotIn("sk-requestsecret", persisted)
            self.assertNotIn("requestsecret12345678901234567890", persisted)
            self.assertNotIn("request-cookie-secret", persisted)
            self.assertNotIn("list-secret-token", persisted)
            self.assertNotIn("verify-secret-token", persisted)
            self.assertNotIn("result-cookie-secret", persisted)
            self.assertNotIn("errorsecret12345678901234567890", persisted)
            self.assertIn("[REDACTED]", persisted)
            persisted_request = json.loads(row["request_json"])
            self.assertEqual(persisted_request["safe"], "metadata")
            self.assertEqual(persisted_request["items"][0]["safe"], "list-metadata")
            self.assertEqual(persisted_request["items"][0]["access_token"], "[REDACTED]")
            self.assertEqual(
                row["request_sha256"],
                "sha256:"
                + hashlib.sha256(row["request_json"].encode("utf-8")).hexdigest(),
            )
            self.assertEqual(
                row["result_sha256"],
                "sha256:"
                + hashlib.sha256(row["result_json"].encode("utf-8")).hexdigest(),
            )

    def test_update_external_effect_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, _db = self._store(tmpdir)
            effect = store.record_external_effect(
                external_effect_id="effect-1",
                task_id="task-1",
                run_id="run-1",
                phase="implementation",
                effect_kind="github_push",
                target="origin/feat",
                content_hash="sha256:content",
                request={"branch": "feat"},
            )

            updated = store.update_external_effect_status(
                effect.external_effect_id,
                status="applied",
                result={"remote": "origin", "ok": True},
                increment_attempt_count=True,
                updated_at="2026-06-24T01:00:00Z",
            )

            self.assertEqual(updated.status, "applied")
            self.assertEqual(updated.result, {"remote": "origin", "ok": True})
            self.assertEqual(updated.result_sha256[:7], "sha256:")
            self.assertEqual(updated.attempt_count, 1)
            self.assertEqual(updated.updated_at, "2026-06-24T01:00:00Z")

    def test_update_external_effect_status_preserves_identity_attempts_and_redacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, _db = self._store(tmpdir)
            effect = store.record_external_effect(
                external_effect_id="effect-update",
                task_id="task-1",
                run_id="run-1",
                phase="implementation",
                effect_kind="github_push",
                target="origin/feat",
                request={"branch": "feat"},
                created_at="2026-06-24T00:00:00Z",
            )

            first = store.update_external_effect_status(
                effect.external_effect_id,
                status="apply_in_progress",
                updated_at="2026-06-24T00:05:00Z",
            )
            second = store.update_external_effect_status(
                effect.external_effect_id,
                status="failed",
                result={
                    "ok": False,
                    "authorization": "Bearer resultsecret12345678901234567890",
                },
                error="push failed with api_key=resultsecret12345678901234567890",
                increment_attempt_count=True,
                updated_at="2026-06-24T00:06:00Z",
            )

            self.assertEqual(first.external_effect_id, effect.external_effect_id)
            self.assertEqual(second.external_effect_id, effect.external_effect_id)
            self.assertEqual(first.idempotency_key, effect.idempotency_key)
            self.assertEqual(second.idempotency_key, effect.idempotency_key)
            self.assertEqual(first.attempt_count, 0)
            self.assertEqual(second.attempt_count, 1)
            self.assertEqual(first.updated_at, "2026-06-24T00:05:00Z")
            self.assertEqual(second.updated_at, "2026-06-24T00:06:00Z")
            self.assertEqual(second.result["authorization"], "[REDACTED]")
            self.assertNotIn("resultsecret12345678901234567890", second.result_json)
            self.assertNotIn("resultsecret12345678901234567890", second.error)
            self.assertIn("[REDACTED]", second.error)

    def test_checkpoint_write_can_reference_external_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, _db = self._store(tmpdir)
            effect = store.record_external_effect(
                external_effect_id="effect-linked",
                task_id="task-1",
                run_id="run-1",
                phase="implementation",
                effect_kind="github_pr",
                target="Hecpac/repo#draft",
                request={"title": "Draft PR"},
            )

            write = store.append_checkpoint_write(
                write_id="write-linked",
                task_id="task-1",
                run_id="run-1",
                phase="implementation",
                write_kind="external_effect_intent",
                write_key="external-effect:effect-linked",
                payload={"external_effect_id": effect.external_effect_id},
                external_effect_id=effect.external_effect_id,
            )
            linked = store.list_checkpoint_writes(
                task_id="task-1",
                run_id="run-1",
                phase="implementation",
                external_effect_id=effect.external_effect_id,
            )

            self.assertEqual(write.external_effect_id, effect.external_effect_id)
            self.assertEqual([row.write_id for row in linked], ["write-linked"])

    def test_recovery_cursor_upsert_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, _db = self._store(tmpdir)
            checkpoint = store.create_phase_checkpoint(
                checkpoint_id="checkpoint-1",
                task_id="task-1",
                run_id="run-1",
                phase="research",
                phase_version=1,
                status="started",
                payload={"phase": "research"},
            )

            first = store.upsert_recovery_cursor(
                recovery_cursor_id="cursor-1",
                task_id="task-1",
                run_id="run-1",
                session_id="session-1",
                phase="research",
                cursor_status="ready_to_resume_phase",
                last_checkpoint_id=checkpoint.checkpoint_id,
                last_write_order=3,
                resume_payload={"step": "draft"},
                updated_at="2026-06-24T00:00:00Z",
            )
            second = store.upsert_recovery_cursor(
                task_id="task-1",
                run_id="run-1",
                phase="synthesis",
                cursor_status="ready_to_start_phase",
                last_checkpoint_id=checkpoint.checkpoint_id,
                last_write_order=4,
                resume_payload={"step": "next"},
                updated_at="2026-06-24T01:00:00Z",
            )
            loaded = store.get_recovery_cursor(task_id="task-1", run_id="run-1")

            self.assertEqual(first.recovery_cursor_id, "cursor-1")
            self.assertEqual(second.recovery_cursor_id, "cursor-1")
            self.assertEqual(loaded.phase, "synthesis")
            self.assertEqual(loaded.cursor_status, "ready_to_start_phase")
            self.assertEqual(loaded.resume_payload, {"step": "next"})
            self.assertEqual(loaded.last_write_order, 4)

    def test_malformed_json_payload_handling_does_not_crash_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, db = self._store(tmpdir)
            store.create_phase_checkpoint(
                checkpoint_id="checkpoint-1",
                task_id="task-1",
                run_id="run-1",
                phase="research",
                phase_version=1,
                status="started",
                payload={"valid": True},
            )
            with db.transaction() as cur:
                cur.execute(
                    """
                    UPDATE phase_checkpoints
                    SET payload_json = ?
                    WHERE checkpoint_id = ?
                    """,
                    ("{not-json", "checkpoint-1"),
                )

            loaded = store.get_phase_checkpoint("checkpoint-1")

            self.assertIsNone(loaded.payload)
            self.assertIsNotNone(loaded.payload_error)

    def test_store_does_not_open_production_db_or_raw_sqlite_connection(self) -> None:
        source = inspect.getsource(f2_store_module)

        self.assertNotIn("sqlite3.connect", source)
        self.assertNotIn("connect_runtime_sqlite", source)
        self.assertNotIn("AppConfig", source)
        self.assertNotIn("data/claw.db", source)
        self.assertNotIn(".claw", source)

    def test_store_uses_runtimedb_connection_and_lock_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            store = F2DurabilityStore(db)

            with patch.object(db, "transaction", wraps=db.transaction) as transaction:
                store.create_phase_checkpoint(
                    task_id="task-1",
                    run_id="run-1",
                    phase="research",
                    phase_version=1,
                    status="started",
                    payload={},
                )
            with patch.object(db, "cursor", wraps=db.cursor) as cursor:
                loaded = store.list_phase_checkpoints(task_id="task-1")

            self.assertGreaterEqual(transaction.call_count, 1)
            self.assertGreaterEqual(cursor.call_count, 1)
            self.assertEqual(len(loaded), 1)

    def test_concurrent_inserts_through_runtimedb_do_not_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, _db = self._store(tmpdir)
            start = threading.Event()
            errors: list[str] = []

            def worker(index: int) -> None:
                start.wait(2)
                try:
                    store.append_checkpoint_write(
                        task_id="task-1",
                        run_id="run-1",
                        phase="verification",
                        write_kind="worker_return",
                        payload={"index": index},
                    )
                except BaseException:
                    errors.append(traceback.format_exc())

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
            for thread in threads:
                thread.start()
            start.set()
            for thread in threads:
                thread.join(5)

            writes = store.list_checkpoint_writes(
                task_id="task-1",
                run_id="run-1",
                phase="verification",
            )
            self.assertEqual(errors, [])
            self.assertEqual(len(writes), 20)
            self.assertEqual([row.write_order for row in writes], list(range(1, 21)))


if __name__ == "__main__":
    unittest.main()
