from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Literal

from claw_v2.sqlite_runtime import (
    WAL_HEAL_RETRY_LIMIT,
    RuntimeDb,
    connect_runtime_sqlite,
    heal_orphaned_wal,
    heal_wal_after_closed_connection,
    heal_wal_after_disk_io,
    make_store_wal_heal,
    note_wal_generation,
    register_wal_heal,
    wal_generation_stamp_missing,
    wal_sidecars_orphaned,
)
from claw_v2.turn_context import (
    CRITICAL_OBSERVE_EVENTS_REQUIRING_TURN_ID,
    current_turn_id,
)

logger = logging.getLogger(__name__)

EventCallback = Callable[[dict], None]
OBSERVE_LOCKED_RETRY_ATTEMPTS = 3
OBSERVE_LOCKED_RETRY_DELAY_SECONDS = 0.1
OBSERVE_SQLITE_BUSY_TIMEOUT_MS = 250
# The off-tick maintenance VACUUM runs on a dedicated connection and SHOULD
# wait for any in-flight emit to finish (rather than drop-fast like emits do),
# so it gets a much longer busy timeout.
OBSERVE_MAINTENANCE_BUSY_TIMEOUT_MS = 30_000
OBSERVE_SPILL_DRAIN_MAX_LINES = 1_000
_SPILL_INSERT_STATUS = Literal["inserted", "already_present", "failed"]

AUDIT_CRITICAL_OBSERVE_EVENTS = frozenset(
    {
        # Approvals / human authorization.
        "approval_created",
        "approval_approved",
        "approval_rejected",
        "approval_expired",
        "approval_archived",
        "approval_pending",
        "approval_required",
        "tier3_approval_required",
        "owner_delegation_approval_required",
        "telegram_imperative_pending_approval",
        "implicit_approval_requires_explicit_approval",
        "approval_detected",
        "approval_cleanup_executed",
        "computer_approval_unavailable",
        "computer_approval_blocked_no_screenshot",
        "computer_approval_pending",
        "computer_approval_resume_blocked",
        "computer_approval_resume_started",
        "computer_approval_screenshot_captured",
        "computer_approval_screenshot_failed",
        "computer_browser_use_approval_required",
        # Tool-use and runtime policy enforcement.
        "sdk_post_tool_use",
        "sdk_post_tool_use_failure",
        "runtime_policy_tool_not_declared",
        "brain_inline_browser_drive_blocked",
        "brain_detached_process_blocked",
        "brain_sdk_agent_dispatch_blocked",
        "brain_tooluse_ledger_started",
        "brain_tooluse_ledger_needs_verification",
        "brain_tooluse_ledger_completed_with_warnings",
        "brain_tooluse_ledger_failed",
        "brain_tooluse_ledger_blocked_unverified_action",
        "brain_tooluse_ledger_verification_failed",
        "f4b2_auto_reprompt_issued",
        "f4b2_auto_reprompt_result",
        "f4b2_auto_reprompt_failed",
        "tool_pivot",
        "critical_action_execution",
        "critical_action_verification",
        # Auth / policy surfaces.
        "web_chat_auth_rejected",
        "web_chat_request_failed",
        # Runtime degradation and critical errors.
        "runtime_db_degraded",
        "runtime_db_persistent_lock_self_heal",
        "daemon_branch_integrity_violation",
        "startup_healthcheck_failed",
        "scheduled_job_error",
        "coordinator_critical_worker_error",
        "coordinator_critical_abort_orphaned_workers",
        "kairos_decide_failed",
        "llm_error",
        "f2_durability_write_failed",
        "p0_telemetry_failed",
    }
)


def is_audit_critical_event(event_type: str, payload: dict | None = None) -> bool:
    if event_type in AUDIT_CRITICAL_OBSERVE_EVENTS:
        return True
    return isinstance(payload, dict) and payload.get("audit_critical") is True


@dataclass(frozen=True, slots=True)
class ObserveSpillDrainResult:
    spill_path: str
    read_lines: int = 0
    inserted: int = 0
    already_present: int = 0
    malformed: int = 0
    failed: int = 0
    removed_lines: int = 0
    remaining_lines: int = 0
    limited: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _SpillRecord:
    raw_line: str
    spill_id: str
    raw_sha256: str
    event_type: str
    payload_json: str
    dropped_at: float | None
    lane: str | None = None
    provider: str | None = None
    model: str | None = None
    trace_id: str | None = None
    root_trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    job_id: str | None = None
    artifact_id: str | None = None


OBSERVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS observe_stream (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    event_type TEXT NOT NULL,
    lane TEXT,
    provider TEXT,
    model TEXT,
    trace_id TEXT,
    root_trace_id TEXT,
    span_id TEXT,
    parent_span_id TEXT,
    job_id TEXT,
    artifact_id TEXT,
    payload TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS observe_spill_drain (
    spill_id TEXT PRIMARY KEY,
    observe_stream_id INTEGER NOT NULL,
    drained_at REAL NOT NULL,
    dropped_at REAL,
    raw_sha256 TEXT NOT NULL
);
"""

OBSERVE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_observe_stream_event_time
    ON observe_stream(event_type, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_observe_stream_trace_id_id
    ON observe_stream(trace_id, id);
CREATE INDEX IF NOT EXISTS idx_observe_stream_job_id_id
    ON observe_stream(job_id, id);
CREATE INDEX IF NOT EXISTS idx_observe_stream_root_trace_id_id
    ON observe_stream(root_trace_id, id);
CREATE INDEX IF NOT EXISTS idx_observe_stream_turn_id
    ON observe_stream(json_extract(payload, '$.turn_id'));
CREATE INDEX IF NOT EXISTS idx_observe_stream_timestamp
    ON observe_stream(timestamp);
"""

# Retention for the immutable event log. The table grows ~3+ rows/min when
# idle and dozens per active turn; without pruning the per-turn receipt
# lookup and every timestamp-window query degrade linearly forever.
OBSERVE_RETENTION_DAYS = 30
OBSERVE_PRUNE_MAX_ROWS = 20_000
# Absolute ceiling on the table independent of row age. The age-only prune
# lets the log grow without bound when most rows are within retention
# (heartbeat/tick plumbing dominates); this caps total size so hot-path
# queries stay bounded. Applied by the off-tick maintenance runner.
OBSERVE_MAX_TOTAL_ROWS = 50_000


class ObserveStream:
    def __init__(self, db_path: Path | str, *, runtime_db: RuntimeDb | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if runtime_db is not None:
            # F1.1a1 production path: share the single RuntimeDb connection + lock
            # (row_factory=False — observe rows are positional tuples). Do NOT set
            # observe's short busy_timeout here: it would change the SHARED
            # connection's timeout for every store. Fast-drop on contention is
            # provided instead by RuntimeDb.try_acquire in _persist_event.
            self._db: RuntimeDb | None = runtime_db
            self._conn = runtime_db.connection_handle(row_factory=False)
            self._lock = runtime_db.lock
        else:
            # Transitional test/back-compat path (not used by main.py).
            self._db = None
            self._conn = connect_runtime_sqlite(self.db_path, row_factory=False)
            register_wal_heal(self.db_path, make_store_wal_heal(self, row_factory=False))
            # Observe events are diagnostic. If another runtime writer owns the DB,
            # drop the event quickly instead of blocking the chat/event loop.
            self._conn.execute(f"PRAGMA busy_timeout={OBSERVE_SQLITE_BUSY_TIMEOUT_MS}")
            self._lock = threading.Lock()
        self._conn.executescript(OBSERVE_SCHEMA)
        self._subscribers: dict[str, list[EventCallback]] = {}
        self._ensure_schema()

    def subscribe(self, event_type: str, callback: EventCallback) -> None:
        """Register a callback to fire whenever `event_type` is emitted.

        Callbacks must be cheap and non-blocking; long work belongs in a
        consumer thread. Exceptions inside callbacks are logged and
        swallowed so they cannot break the emit path.
        """
        self._subscribers.setdefault(event_type, []).append(callback)

    def emit(
        self,
        event_type: str,
        *,
        lane: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        trace_id: str | None = None,
        root_trace_id: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        job_id: str | None = None,
        artifact_id: str | None = None,
        payload: dict | None = None,
    ) -> None:
        # Wave 3.5: defense-in-depth — scrub system-reminder markers from the
        # payload BEFORE we persist or fan out to subscribers, so a leak that
        # bypassed the chat sanitizer still doesn't end up on disk.
        from claw_v2.leak_scrub import scrub_for_persistence
        from claw_v2.redaction import redact_sensitive

        clean_payload = redact_sensitive(scrub_for_persistence(payload or {}), limit=0)
        audit_critical = is_audit_critical_event(event_type, clean_payload)
        if audit_critical and isinstance(clean_payload, dict):
            clean_payload.setdefault("audit_critical", True)
        # P0-B: stamp the active turn_id (if any) on every persisted payload so
        # behavior receipts can join observe, task ledger, and approval rows by
        # one column instead of fragile timestamp windows. When a critical
        # event fires WITHOUT a turn_id context, emit a sibling
        # ``turn_id_missing`` so the gap is visible.
        active_turn_id = current_turn_id()
        if isinstance(clean_payload, dict):
            if active_turn_id and "turn_id" not in clean_payload:
                clean_payload["turn_id"] = active_turn_id
            emit_turn_id_missing = (
                event_type in CRITICAL_OBSERVE_EVENTS_REQUIRING_TURN_ID
                and "turn_id" not in clean_payload
                and event_type != "turn_id_missing"
            )
        else:
            emit_turn_id_missing = False
        persisted = self._persist_event(
            event_type,
            lane=lane,
            provider=provider,
            model=model,
            trace_id=trace_id,
            root_trace_id=root_trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            job_id=job_id,
            artifact_id=artifact_id,
            clean_payload=clean_payload,
            audit_critical=audit_critical,
        )
        # Dispatch in-process subscribers even if the diagnostic write was
        # dropped: a transient SQLite lock must not swallow task-completion
        # notifications (autonomous_task_completed/failed) wired as subscribers.
        callbacks = self._subscribers.get(event_type)
        if callbacks:
            event_payload = clean_payload
            for cb in callbacks:
                try:
                    cb(event_payload)
                except Exception:
                    logger.exception("observe subscriber for %s failed", event_type)
        if not persisted:
            return
        if emit_turn_id_missing:
            # Recurse with a sentinel payload; the early `event_type !=
            # "turn_id_missing"` guard above prevents infinite recursion.
            self.emit(
                "turn_id_missing",
                payload={"origin_event": event_type},
            )

    def _persist_event(
        self,
        event_type: str,
        *,
        lane: str | None,
        provider: str | None,
        model: str | None,
        trace_id: str | None,
        root_trace_id: str | None,
        span_id: str | None,
        parent_span_id: str | None,
        job_id: str | None,
        artifact_id: str | None,
        clean_payload: dict,
        audit_critical: bool,
    ) -> bool:
        payload_json = json.dumps(clean_payload)
        if self._db is not None:
            # F1.1a1 production path: write through the shared RuntimeDb
            # connection with fast-drop-on-contention (try_acquire), so a busy
            # store never blocks the chat/event loop on the shared lock.
            return self._persist_event_shared(
                event_type,
                payload_json,
                lane=lane,
                provider=provider,
                model=model,
                trace_id=trace_id,
                root_trace_id=root_trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                job_id=job_id,
                artifact_id=artifact_id,
                audit_critical=audit_critical,
            )
        # M5: bounded heal burst — concurrent heals can re-close the connection
        # during a post-heal retry, so tolerate a run of heals (not exactly one)
        # before dropping. Heals coalesce, so the run converges quickly.
        heals = 0
        attempt = 0
        while attempt < OBSERVE_LOCKED_RETRY_ATTEMPTS:
            attempt += 1
            try:
                with self._lock:
                    self._conn.execute(
                        """
                        INSERT INTO observe_stream (
                            event_type, lane, provider, model,
                            trace_id, root_trace_id, span_id, parent_span_id, job_id, artifact_id,
                            payload
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_type,
                            lane,
                            provider,
                            model,
                            trace_id,
                            root_trace_id,
                            span_id,
                            parent_span_id,
                            job_id,
                            artifact_id,
                            payload_json,
                        ),
                    )
                    self._conn.commit()
                # Generation drift check (live drill 2026-06-12): a victim of
                # an external sidecar swap keeps writing 'successfully' into
                # the orphaned inode WITHOUT lock errors (void writes), so the
                # locked-exhaust hook alone is blind. One stat per persist:
                # stamp the generation on the first write after (re)connect,
                # then heal as soon as the on-disk wal stops being ours.
                if wal_generation_stamp_missing(self.db_path):
                    note_wal_generation(self.db_path)
                elif wal_sidecars_orphaned(self.db_path):
                    heal_orphaned_wal(self.db_path)
                return True
            except sqlite3.OperationalError as exc:
                try:
                    with self._lock:
                        self._conn.rollback()
                except Exception:
                    logger.debug("observe rollback failed after SQLite write error", exc_info=True)
                if "locked" not in str(exc).lower():
                    if heals < WAL_HEAL_RETRY_LIMIT and heal_wal_after_disk_io(
                        self.db_path, exc, context="ObserveStream._persist_event"
                    ):
                        heals += 1
                        attempt = 0
                        continue
                    raise
                if attempt >= OBSERVE_LOCKED_RETRY_ATTEMPTS and heals < WAL_HEAL_RETRY_LIMIT:
                    # T10 (2026-06-12): persistent locks with the -wal sidecar
                    # gone mean this process is wedged on orphaned WAL files.
                    # Heal (reopen every registered connection) and grant a
                    # fresh retry round before dropping anything.
                    if heal_orphaned_wal(self.db_path):
                        heals += 1
                        attempt = 0
                        continue
                if attempt >= OBSERVE_LOCKED_RETRY_ATTEMPTS:
                    logger.warning(
                        "dropping observe event after locked database retries: %s",
                        event_type,
                        exc_info=True,
                    )
                    # AM-OBSDROP (2026-06-12): the stream is the audit-trail
                    # invariant — a dropped event must leave a recoverable
                    # trace on disk, not vanish.
                    self._spill_dropped_event(
                        event_type,
                        lane=lane,
                        provider=provider,
                        model=model,
                        trace_id=trace_id,
                        root_trace_id=root_trace_id,
                        span_id=span_id,
                        parent_span_id=parent_span_id,
                        job_id=job_id,
                        artifact_id=artifact_id,
                        audit_critical=audit_critical,
                        payload_json=payload_json,
                    )
                    return False
                time.sleep(OBSERVE_LOCKED_RETRY_DELAY_SECONDS * attempt)
            except sqlite3.ProgrammingError as exc:
                if heals < WAL_HEAL_RETRY_LIMIT and heal_wal_after_closed_connection(
                    self.db_path, exc, context="ObserveStream._persist_event"
                ):
                    heals += 1
                    attempt = 0
                    continue
                raise
        return False

    def _persist_event_shared(
        self,
        event_type: str,
        payload_json: str,
        *,
        audit_critical: bool = False,
        **columns: str | None,
    ) -> bool:
        """Persist via the shared RuntimeDb connection (F1.1a1 production path).

        Acquire the shared lock NON-BLOCKING (``try_acquire``) so a busy store
        never blocks the chat/event loop. A bounded run of attempts gives brief
        contention a chance to clear; if the lock stays held — or the write
        errors — spill the event to JSONL (recoverable; the audit-trail
        invariant) and drop, rather than blocking indefinitely or losing it.
        """
        for attempt in range(1, OBSERVE_LOCKED_RETRY_ATTEMPTS + 1):
            with self._db.try_acquire() as acquired:  # type: ignore[union-attr]
                if acquired:
                    try:
                        self._conn.execute(
                            """
                            INSERT INTO observe_stream (
                                event_type, lane, provider, model,
                                trace_id, root_trace_id, span_id, parent_span_id, job_id, artifact_id,
                                payload
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                event_type,
                                columns.get("lane"),
                                columns.get("provider"),
                                columns.get("model"),
                                columns.get("trace_id"),
                                columns.get("root_trace_id"),
                                columns.get("span_id"),
                                columns.get("parent_span_id"),
                                columns.get("job_id"),
                                columns.get("artifact_id"),
                                payload_json,
                            ),
                        )
                        self._conn.commit()
                        return True
                    except sqlite3.Error:
                        try:
                            self._conn.rollback()
                        except Exception:
                            logger.debug("observe rollback failed (shared conn)", exc_info=True)
                        logger.warning(
                            "dropping observe event after shared-connection write error: %s",
                            event_type,
                            exc_info=True,
                        )
                        break  # a real write error: spill rather than spin
            # Lock contended — brief bounded backoff, then retry. Never block
            # indefinitely; exhausting the attempts spills below.
            if attempt < OBSERVE_LOCKED_RETRY_ATTEMPTS:
                time.sleep(OBSERVE_LOCKED_RETRY_DELAY_SECONDS * attempt)
        self._spill_dropped_event(
            event_type,
            payload_json=payload_json,
            audit_critical=audit_critical,
            **columns,
        )
        return False

    def _spill_dropped_event(
        self,
        event_type: str,
        *,
        payload_json: str,
        audit_critical: bool = False,
        **columns: str | None,
    ) -> None:
        """Append a dropped event as a JSONL line next to the DB.

        Best-effort: spilling must never raise into the emit path. The file
        is the recovery source for events the locked DB rejected; `think`
        tooling (or a future drain job) can replay it.
        """
        try:
            line = json.dumps(
                {
                    "dropped_at": time.time(),
                    "event_type": event_type,
                    "spill_id": f"obs-spill:{uuid.uuid4().hex}",
                    **({"audit_critical": True} if audit_critical else {}),
                    **{k: v for k, v in columns.items() if v is not None},
                    "payload": payload_json,
                },
                sort_keys=True,
            )
            self._append_spill_line(line)
        except Exception:
            logger.debug("observe spill write failed", exc_info=True)

    def _spill_path(self) -> Path:
        return self.db_path.with_suffix(".spill.jsonl")

    @contextlib.contextmanager
    def _spill_file_lock(self):
        spill_path = self._spill_path()
        lock_path = spill_path.with_suffix(spill_path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _append_spill_line(self, line: str) -> None:
        spill_path = self._spill_path()
        with self._spill_file_lock():
            with spill_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())

    def _ensure_spill_occurrence_ids(self, raw_lines: list[str]) -> tuple[list[str], bool]:
        normalized_lines: list[str] = []
        changed = False
        for raw_line in raw_lines:
            normalized_line = self._line_with_spill_occurrence_id(raw_line)
            normalized_lines.append(normalized_line)
            changed = changed or normalized_line != raw_line
        return normalized_lines, changed

    def _line_with_spill_occurrence_id(self, raw_line: str) -> str:
        try:
            parsed = json.loads(raw_line)
        except json.JSONDecodeError:
            return raw_line
        if not isinstance(parsed, dict):
            return raw_line
        parsed_spill_id = parsed.get("spill_id")
        if isinstance(parsed_spill_id, str) and parsed_spill_id:
            return raw_line
        if self._parse_spill_record(raw_line, line_index=0) is None:
            return raw_line
        parsed["spill_id"] = f"obs-spill:{uuid.uuid4().hex}"
        return json.dumps(parsed, sort_keys=True)

    def drain_spill(
        self,
        *,
        max_lines: int = OBSERVE_SPILL_DRAIN_MAX_LINES,
        max_attempts: int = OBSERVE_LOCKED_RETRY_ATTEMPTS,
    ) -> ObserveSpillDrainResult:
        """Replay spilled observe events into ``observe_stream`` idempotently.

        The spill file is append-only on the emit path. Drain removes only lines
        whose event insert and dedup marker are already durable, and it compacts
        through a temp file + atomic replace under the same spill-file lock used
        by appenders. Malformed or currently uninsertable lines stay in place.
        """
        spill_path = self._spill_path()
        if not spill_path.exists():
            return ObserveSpillDrainResult(spill_path=str(spill_path))
        try:
            with self._spill_file_lock():
                if not spill_path.exists():
                    return ObserveSpillDrainResult(spill_path=str(spill_path))
                raw_lines = spill_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            logger.exception("observe spill drain could not read %s", spill_path)
            return ObserveSpillDrainResult(spill_path=str(spill_path), failed=1)

        limit = max(0, int(max_lines))
        lines_to_process = raw_lines[:limit]
        limited = len(raw_lines) > len(lines_to_process)
        inserted = already_present = malformed = failed = 0
        removable_line_indexes: set[int] = set()
        for line_index, raw_line in enumerate(lines_to_process):
            record = self._parse_spill_record(raw_line, line_index=line_index)
            if record is None:
                malformed += 1
                continue
            status = self._drain_spill_record(record, max_attempts=max_attempts)
            if status == "inserted":
                inserted += 1
                removable_line_indexes.add(line_index)
            elif status == "already_present":
                already_present += 1
                removable_line_indexes.add(line_index)
            else:
                failed += 1

        removed_lines = 0
        remaining_lines = len(raw_lines)
        if removable_line_indexes:
            removed_lines, remaining_lines = self._compact_spill(raw_lines, removable_line_indexes)
        return ObserveSpillDrainResult(
            spill_path=str(spill_path),
            read_lines=len(lines_to_process),
            inserted=inserted,
            already_present=already_present,
            malformed=malformed,
            failed=failed,
            removed_lines=removed_lines,
            remaining_lines=remaining_lines,
            limited=limited,
        )

    def _parse_spill_record(self, raw_line: str, *, line_index: int) -> _SpillRecord | None:
        try:
            parsed = json.loads(raw_line)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        event_type = parsed.get("event_type")
        payload_raw = parsed.get("payload")
        if not isinstance(event_type, str) or not event_type:
            return None
        if not isinstance(payload_raw, str):
            return None
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        dropped_at_raw = parsed.get("dropped_at")
        dropped_at = float(dropped_at_raw) if isinstance(dropped_at_raw, (int, float)) else None
        text_columns = {
            name: parsed.get(name)
            for name in (
                "lane",
                "provider",
                "model",
                "trace_id",
                "root_trace_id",
                "span_id",
                "parent_span_id",
                "job_id",
                "artifact_id",
            )
        }
        if any(value is not None and not isinstance(value, str) for value in text_columns.values()):
            return None
        raw_sha256 = hashlib.sha256(raw_line.encode("utf-8")).hexdigest()
        parsed_spill_id = parsed.get("spill_id")
        if isinstance(parsed_spill_id, str) and parsed_spill_id:
            spill_id = parsed_spill_id
        else:
            occurrence_material = f"{line_index}:{raw_sha256}".encode("utf-8")
            spill_id = f"legacy-sha256:{hashlib.sha256(occurrence_material).hexdigest()}"
        return _SpillRecord(
            raw_line=raw_line,
            spill_id=spill_id,
            raw_sha256=raw_sha256,
            event_type=event_type,
            payload_json=payload_raw,
            dropped_at=dropped_at,
            **text_columns,
        )

    def _drain_spill_record(
        self, record: _SpillRecord, *, max_attempts: int
    ) -> _SPILL_INSERT_STATUS:
        attempts = max(1, int(max_attempts))
        for attempt in range(1, attempts + 1):
            if self._db is not None:
                with self._db.try_acquire() as acquired:
                    if acquired:
                        return self._insert_spill_record_locked(record)
            else:
                acquired = self._lock.acquire(blocking=False)
                if acquired:
                    try:
                        return self._insert_spill_record_locked(record)
                    finally:
                        self._lock.release()
            if attempt < attempts:
                time.sleep(OBSERVE_LOCKED_RETRY_DELAY_SECONDS * attempt)
        return "failed"

    def _insert_spill_record_locked(self, record: _SpillRecord) -> _SPILL_INSERT_STATUS:
        try:
            existing = self._conn.execute(
                "SELECT observe_stream_id FROM observe_spill_drain WHERE spill_id = ?",
                (record.spill_id,),
            ).fetchone()
            if existing is not None:
                return "already_present"
            cursor = self._conn.execute(
                """
                INSERT INTO observe_stream (
                    event_type, lane, provider, model,
                    trace_id, root_trace_id, span_id, parent_span_id, job_id, artifact_id,
                    payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.event_type,
                    record.lane,
                    record.provider,
                    record.model,
                    record.trace_id,
                    record.root_trace_id,
                    record.span_id,
                    record.parent_span_id,
                    record.job_id,
                    record.artifact_id,
                    record.payload_json,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO observe_spill_drain (
                    spill_id, observe_stream_id, drained_at, dropped_at, raw_sha256
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.spill_id,
                    int(cursor.lastrowid),
                    time.time(),
                    record.dropped_at,
                    record.raw_sha256,
                ),
            )
            self._conn.commit()
            return "inserted"
        except sqlite3.IntegrityError:
            try:
                self._conn.rollback()
            except Exception:
                logger.debug("observe spill drain rollback failed after duplicate", exc_info=True)
            return "already_present"
        except sqlite3.Error:
            try:
                self._conn.rollback()
            except Exception:
                logger.debug("observe spill drain rollback failed", exc_info=True)
            logger.warning(
                "observe spill drain could not insert event_type=%s",
                record.event_type,
                exc_info=True,
            )
            return "failed"

    def _compact_spill(
        self, snapshot_lines: list[str], removable_line_indexes: set[int]
    ) -> tuple[int, int]:
        spill_path = self._spill_path()
        with self._spill_file_lock():
            if not spill_path.exists():
                return 0, 0
            raw_lines = spill_path.read_text(encoding="utf-8").splitlines()
            if raw_lines[: len(snapshot_lines)] != snapshot_lines:
                logger.warning("observe spill drain skipped compaction after concurrent mutation")
                return 0, len(raw_lines)
            remaining = [
                line for index, line in enumerate(raw_lines) if index not in removable_line_indexes
            ]
            removed = len(raw_lines) - len(remaining)
            if not removed:
                return 0, len(raw_lines)
            remaining, _ = self._ensure_spill_occurrence_ids(remaining)
            self._replace_spill_lines_locked(spill_path, remaining)
            return removed, len(remaining)

    def _replace_spill_lines_locked(self, spill_path: Path, lines: list[str]) -> None:
        if lines:
            tmp_path = spill_path.with_name(f".{spill_path.name}.tmp")
            with tmp_path.open("w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, spill_path)
        else:
            spill_path.unlink(missing_ok=True)
        self._fsync_spill_parent(spill_path)

    def _fsync_spill_parent(self, spill_path: Path) -> None:
        try:
            dir_fd = os.open(str(spill_path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            logger.debug("observe spill drain parent fsync failed", exc_info=True)

    def _ensure_schema(self) -> None:
        existing = {
            row[1] for row in self._conn.execute("PRAGMA table_info(observe_stream)").fetchall()
        }
        for column in (
            "trace_id",
            "root_trace_id",
            "span_id",
            "parent_span_id",
            "job_id",
            "artifact_id",
        ):
            if column not in existing:
                self._conn.execute(f"ALTER TABLE observe_stream ADD COLUMN {column} TEXT")
        self._conn.executescript(OBSERVE_INDEXES)
        self._conn.commit()

    def prune(
        self,
        *,
        retention_days: int = OBSERVE_RETENTION_DAYS,
        max_rows: int = OBSERVE_PRUNE_MAX_ROWS,
        max_total_rows: int | None = None,
    ) -> int:
        """Delete events older than the retention window, bounded per call.

        The LIMIT keeps each sweep short so the scheduler tick that runs it
        never stalls; a backlog drains across consecutive runs.

        When ``max_total_rows`` is set, a second bounded DELETE after the age
        sweep caps the absolute table size (keeping the highest ids), so the
        log cannot grow without bound when most rows are within retention.
        That second sweep is itself bounded by ``max_rows`` per call.
        """
        with self._lock:
            cursor = self._conn.execute(
                """
                DELETE FROM observe_stream
                WHERE id IN (
                    SELECT id FROM observe_stream
                    WHERE timestamp < datetime('now', ?)
                    ORDER BY id
                    LIMIT ?
                )
                """,
                (f"-{int(retention_days)} days", int(max_rows)),
            )
            deleted = int(cursor.rowcount or 0)
            if max_total_rows is not None:
                cursor = self._conn.execute(
                    """
                    DELETE FROM observe_stream
                    WHERE id IN (
                        SELECT id FROM observe_stream
                        WHERE id NOT IN (
                            SELECT id FROM observe_stream
                            ORDER BY id DESC
                            LIMIT ?
                        )
                        ORDER BY id
                        LIMIT ?
                    )
                    """,
                    (int(max_total_rows), int(max_rows)),
                )
                deleted += int(cursor.rowcount or 0)
            self._conn.commit()
            return deleted

    def maintenance_vacuum(self) -> None:
        """Reclaim freed pages by rewriting the database file.

        VACUUM is blocking and needs ~2x free disk, and it cannot run inside
        a transaction, so it MUST run off-tick (background runner /
        maintenance window) — never inline in ``daemon.tick``. The AST
        tripwire ``test_vacuum_only_runs_off_tick`` enforces that.

        Review #115-1: runs on a DEDICATED short-lived connection so it never
        holds ``self._lock``, which ``emit`` needs. A multi-second rewrite on a
        large DB must not block the chat/event loop; SQLite serializes VACUUM's
        write lock against in-flight emits via ``busy_timeout`` (emits drop
        fast on contention, by design). In WAL mode VACUUM writes the rewritten
        pages to the WAL, so a checkpoint(TRUNCATE) afterwards flushes them into
        the main file and truncates the WAL.
        """
        conn = connect_runtime_sqlite(self.db_path, row_factory=False)
        try:
            conn.execute(f"PRAGMA busy_timeout={OBSERVE_MAINTENANCE_BUSY_TIMEOUT_MS}")
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

    def cache_summary(self, hours: int = 24) -> dict:
        """Return prompt cache stats for the last N hours."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT payload
                FROM observe_stream
                WHERE event_type = 'prompt_cache'
                  AND timestamp > datetime('now', ?)
                """,
                (f"-{hours} hours",),
            ).fetchall()
        total_input = 0
        total_cache_read = 0
        total_cache_create = 0
        for (raw,) in rows:
            data = json.loads(raw)
            total_input += data.get("input_tokens", 0)
            total_cache_read += data.get("cache_read_tokens", 0)
            total_cache_create += data.get("cache_create_tokens", 0)
        hit_ratio = total_cache_read / max(total_input, 1) if total_input else 0.0
        estimated_savings_pct = round(hit_ratio * 75, 1)
        return {
            "requests": len(rows),
            "total_input_tokens": total_input,
            "cache_read_tokens": total_cache_read,
            "cache_create_tokens": total_cache_create,
            "hit_ratio": round(hit_ratio, 3),
            "estimated_savings_pct": estimated_savings_pct,
        }

    def total_cost_today(self, *, providers: set[str] | None = None) -> float:
        provider_filter = ""
        params: tuple[object, ...] = ()
        if providers is not None:
            normalized = sorted(provider for provider in providers if provider)
            if not normalized:
                return 0.0
            placeholders = ",".join("?" for _ in normalized)
            provider_filter = f" AND provider IN ({placeholders})"
            params = tuple(normalized)
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT COALESCE(SUM(json_extract(payload, '$.cost_estimate')), 0.0)
                FROM observe_stream
                WHERE event_type IN ('llm_response', 'llm_fallback', 'llm_failed_spend')
                  AND timestamp >= date('now', 'start of day')
                  {provider_filter}
                """,
                params,
            ).fetchone()
        return float(row[0]) if row else 0.0

    def cost_since(self, since_epoch: float) -> float:
        """Sum of LLM spend recorded at or after ``since_epoch`` (unix time).

        AM-LOOPCOST (2026-06-12): unlike total_cost_today this is monotonic —
        it never resets at midnight, so a budget guard anchored to a loop's
        start timestamp cannot silently disarm mid-run.
        """
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(json_extract(payload, '$.cost_estimate')), 0.0)
                FROM observe_stream
                WHERE event_type IN ('llm_response', 'llm_fallback', 'llm_failed_spend')
                  AND timestamp >= datetime(?, 'unixepoch')
                """,
                (float(since_epoch),),
            ).fetchone()
        return float(row[0]) if row else 0.0

    def has_unknown_billable_cost_today(self, *, providers: set[str] | None = None) -> bool:
        """True if any billable LLM call today had an unpriced (cost_unknown) model.

        Lets the daily cost gate fail closed on unmetered billable spend instead
        of treating cost_unknown as zero (2026-05-31 audit H5).
        """
        provider_filter = ""
        params: tuple[object, ...] = ()
        if providers is not None:
            normalized = sorted(provider for provider in providers if provider)
            if not normalized:
                return False
            placeholders = ",".join("?" for _ in normalized)
            provider_filter = f" AND provider IN ({placeholders})"
            params = tuple(normalized)
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT 1
                FROM observe_stream
                WHERE timestamp >= date('now', 'start of day')
                  AND (
                      (event_type IN ('llm_response', 'llm_fallback')
                       AND json_extract(payload, '$.cost_unknown') = 1)
                      OR event_type = 'cost_metering_unknown'
                  )
                  {provider_filter}
                LIMIT 1
                """,
                params,
            ).fetchone()
        return row is not None

    def cost_per_agent_today(self) -> dict[str, float]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT COALESCE(
                           json_extract(payload, '$.agent_name'),
                           json_extract(payload, '$.sub_agent')
                       ) as agent,
                       COALESCE(SUM(json_extract(payload, '$.cost_estimate')), 0.0) as cost
                FROM observe_stream
                WHERE event_type = 'llm_decision'
                  AND timestamp >= date('now', 'start of day')
                GROUP BY agent
                """,
            ).fetchall()
        return {row[0]: row[1] for row in rows if row[0]}

    def spending_today(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT lane, provider, model,
                       COALESCE(SUM(json_extract(payload, '$.cost_estimate')), 0.0) as cost,
                       COUNT(*) as requests
                FROM observe_stream
                WHERE event_type = 'llm_decision'
                  AND timestamp >= date('now', 'start of day')
                GROUP BY lane, provider, model
                ORDER BY cost DESC
                """,
            ).fetchall()
        by_lane: dict[str, float] = {}
        by_provider: dict[str, float] = {}
        by_model: dict[str, float] = {}
        rows_payload: list[dict] = []
        total = 0.0
        for lane, provider, model, cost, requests in rows:
            cost = float(cost or 0.0)
            total += cost
            lane_key = lane or "unknown"
            provider_key = provider or "unknown"
            model_key = model or "unknown"
            by_lane[lane_key] = by_lane.get(lane_key, 0.0) + cost
            by_provider[provider_key] = by_provider.get(provider_key, 0.0) + cost
            by_model[model_key] = by_model.get(model_key, 0.0) + cost
            rows_payload.append(
                {
                    "lane": lane_key,
                    "provider": provider_key,
                    "model": model_key,
                    "requests": int(requests or 0),
                    "cost": round(cost, 6),
                }
            )
        return {
            "total": round(total, 6),
            "by_lane": {key: round(value, 6) for key, value in sorted(by_lane.items())},
            "by_provider": {key: round(value, 6) for key, value in sorted(by_provider.items())},
            "by_model": {key: round(value, 6) for key, value in sorted(by_model.items())},
            "rows": rows_payload,
        }

    def recent_events(self, limit: int = 20, *, event_type: str | None = None) -> list[dict]:
        query = """
            SELECT event_type, lane, provider, model,
                   trace_id, root_trace_id, span_id, parent_span_id, job_id, artifact_id,
                   payload, timestamp
            FROM observe_stream
        """
        params: tuple[object, ...]
        if event_type:
            query += " WHERE event_type = ?\n            ORDER BY id DESC\n            LIMIT ?"
            params = (event_type, limit)
        else:
            query += " ORDER BY id DESC\n            LIMIT ?"
            params = (limit,)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_event_row_to_dict(row) for row in rows]

    def trace_events(self, trace_id: str, *, limit: int | None = None) -> list[dict]:
        query = """
            SELECT event_type, lane, provider, model,
                   trace_id, root_trace_id, span_id, parent_span_id, job_id, artifact_id,
                   payload, timestamp
            FROM observe_stream
            WHERE trace_id = ?
            ORDER BY id ASC
        """
        params: tuple[object, ...] = (trace_id,)
        if limit is not None:
            query += " LIMIT ?"
            params = (trace_id, limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_event_row_to_dict(row) for row in rows]

    def job_events(self, job_id: str, *, limit: int | None = None) -> list[dict]:
        query = """
            SELECT event_type, lane, provider, model,
                   trace_id, root_trace_id, span_id, parent_span_id, job_id, artifact_id,
                   payload, timestamp
            FROM observe_stream
            WHERE job_id = ?
            ORDER BY id ASC
        """
        params: tuple[object, ...] = (job_id,)
        if limit is not None:
            query += " LIMIT ?"
            params = (job_id, limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_event_row_to_dict(row) for row in rows]


def _event_row_to_dict(row: sqlite3.Row | tuple) -> dict:
    return {
        "event_type": row[0],
        "lane": row[1],
        "provider": row[2],
        "model": row[3],
        "trace_id": row[4],
        "root_trace_id": row[5],
        "span_id": row[6],
        "parent_span_id": row[7],
        "job_id": row[8],
        "artifact_id": row[9],
        "payload": json.loads(row[10]),
        "timestamp": row[11],
    }
