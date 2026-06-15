"""Capability Grants — explicit, scoped, auditable autonomy grants.

Today the bot picks up ad-hoc autonomy via ``_handle_autonomy_grant_response``
when Hector types phrases like "tienes autonomía" / "full autonomy". The
grant is implicit, untyped (boolean), unscoped (covers everything until
the next session reset), and not durably auditable.

Capability Grants make the grant a first-class persistent object:
  - **scope** declares exactly what's authorised (a tool, a domain, a
    path glob),
  - **grantor** records who issued it (user / system / kairos),
  - **ttl** caps the grant in time (None = until revoked),
  - **revoke** is a first-class operation with a reason,
  - every state change emits an observe event so post-mortems and
    the learning loop see grant-driven decisions.

This module ships the storage + API only. Wiring grants into
``ApprovalGate`` / ``ToolRegistry`` is a follow-up PR; the MVP is the
data layer + tests so the next wave can read/write without churning the
schema.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from claw_v2.sqlite_runtime import (
    RuntimeDb,
    connect_runtime_sqlite,
    make_store_wal_heal,
    register_wal_heal,
)

logger = logging.getLogger(__name__)


CAPABILITY_GRANT_SCHEMA = """
CREATE TABLE IF NOT EXISTS capability_grants (
    grant_id TEXT PRIMARY KEY,
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('tool', 'domain', 'path', 'session')),
    scope_target TEXT NOT NULL,
    grantor TEXT NOT NULL,
    granted_at REAL NOT NULL,
    expires_at REAL,
    revoked_at REAL,
    revoke_reason TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_capability_grants_scope
    ON capability_grants(scope_kind, scope_target);

CREATE INDEX IF NOT EXISTS idx_capability_grants_active
    ON capability_grants(revoked_at, expires_at);
"""


VALID_SCOPE_KINDS = frozenset({"tool", "domain", "path", "session"})


@dataclass(slots=True)
class GrantScope:
    """A scope is the precise capability the grant authorises.

    ``kind`` selects the resolver:
      - ``tool``: ``target`` is a tool name (``Bash``, ``Write``, …).
      - ``domain``: ``target`` is a registered domain (``x.com``,
        ``linkedin.com``); subdomain matching is left to the caller.
      - ``path``: ``target`` is a filesystem glob (``/etc/*``).
      - ``session``: ``target`` is a session id (``tg-...``).

    Matching uses ``fnmatch`` so ``target`` may contain wildcards.
    """

    kind: str
    target: str

    def matches(self, *, kind: str, target: str) -> bool:
        if kind != self.kind:
            return False
        return fnmatch.fnmatchcase(target, self.target)


@dataclass(slots=True)
class CapabilityGrant:
    grant_id: str
    scope: GrantScope
    grantor: str
    granted_at: float
    expires_at: float | None = None
    revoked_at: float | None = None
    revoke_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_active(self, *, now: float | None = None) -> bool:
        ts = float(now) if now is not None else time.time()
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and ts >= self.expires_at:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Flatten scope for readability while keeping the dataclass
        # interface ergonomic in code.
        data["scope_kind"] = self.scope.kind
        data["scope_target"] = self.scope.target
        return data


class CapabilityGrantStore:
    """Durable store for capability grants.

    Backed by SQLite on the runtime DB so grants survive restarts and
    join naturally with ``observe_stream``, ``agent_tasks``, and other
    runtime state. All mutating operations emit observability events
    when an ``observe`` instance is provided.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        observe: Any | None = None,
        runtime_db: RuntimeDb | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if runtime_db is not None:
            # F1.1a1 production path: share the single RuntimeDb connection + lock.
            self._db: RuntimeDb | None = runtime_db
            self._conn = runtime_db.connection_handle(row_factory=True)
            self._lock = runtime_db.lock
        else:
            # Transitional test/back-compat path (not used by main.py).
            self._db = None
            self._conn = connect_runtime_sqlite(self.db_path)
            register_wal_heal(self.db_path, make_store_wal_heal(self))
            self._lock = threading.Lock()
        self.observe = observe
        with self._lock:
            self._conn.executescript(CAPABILITY_GRANT_SCHEMA)
            self._conn.commit()

    # ----- mutation API ------------------------------------------------

    def grant(
        self,
        scope: GrantScope,
        *,
        grantor: str,
        ttl_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CapabilityGrant:
        if scope.kind not in VALID_SCOPE_KINDS:
            raise ValueError(
                f"invalid scope kind {scope.kind!r}; expected one of {sorted(VALID_SCOPE_KINDS)}"
            )
        if not scope.target:
            raise ValueError("scope target must be non-empty")
        if not grantor:
            raise ValueError("grantor must be non-empty (user/system/kairos/…)")
        now = time.time()
        grant_id = secrets.token_hex(8)
        record = CapabilityGrant(
            grant_id=grant_id,
            scope=scope,
            grantor=grantor,
            granted_at=now,
            expires_at=(now + float(ttl_seconds)) if ttl_seconds is not None else None,
            revoked_at=None,
            revoke_reason=None,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO capability_grants
                    (grant_id, scope_kind, scope_target, grantor,
                     granted_at, expires_at, revoked_at, revoke_reason, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.grant_id,
                    record.scope.kind,
                    record.scope.target,
                    record.grantor,
                    record.granted_at,
                    record.expires_at,
                    record.revoked_at,
                    record.revoke_reason,
                    json.dumps(record.metadata, ensure_ascii=False),
                ),
            )
            self._conn.commit()
        self._emit("capability_grant_issued", record.to_dict())
        return record

    def revoke(self, grant_id: str, *, reason: str = "manual") -> CapabilityGrant | None:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE capability_grants
                SET revoked_at = ?, revoke_reason = ?
                WHERE grant_id = ? AND revoked_at IS NULL
                """,
                (now, reason, grant_id),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
        record = self.get(grant_id)
        if record is not None:
            self._emit("capability_grant_revoked", record.to_dict())
        return record

    # ----- read API ----------------------------------------------------

    def get(self, grant_id: str) -> CapabilityGrant | None:
        with self._lock:  # F1.1a1: fetch under the shared lock; build record outside
            row = self._conn.execute(
                "SELECT * FROM capability_grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def list_active(self, *, now: float | None = None) -> list[CapabilityGrant]:
        ts = float(now) if now is not None else time.time()
        with self._lock:  # F1.1a1: fetch under the shared lock; build records outside
            rows = self._conn.execute(
                """
                SELECT * FROM capability_grants
                WHERE revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY granted_at DESC
                """,
                (ts,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def is_granted(self, *, kind: str, target: str, now: float | None = None) -> bool:
        """Return True if at least one active grant covers ``(kind, target)``.

        A "session" scope with ``target='*'`` acts as a wildcard grant and
        matches any specific session id; the same applies to other kinds
        when ``scope.target`` contains glob characters.
        """
        for grant in self.list_active(now=now):
            if grant.scope.matches(kind=kind, target=target):
                return True
        return False

    def find_grants_for(
        self, *, kind: str, target: str, now: float | None = None
    ) -> list[CapabilityGrant]:
        return [g for g in self.list_active(now=now) if g.scope.matches(kind=kind, target=target)]

    # ----- internals ---------------------------------------------------

    def _row_to_record(self, row: Any) -> CapabilityGrant:
        try:
            metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        except Exception:
            metadata = {}
        return CapabilityGrant(
            grant_id=str(row["grant_id"]),
            scope=GrantScope(kind=str(row["scope_kind"]), target=str(row["scope_target"])),
            grantor=str(row["grantor"]),
            granted_at=float(row["granted_at"]),
            expires_at=float(row["expires_at"]) if row["expires_at"] is not None else None,
            revoked_at=float(row["revoked_at"]) if row["revoked_at"] is not None else None,
            revoke_reason=row["revoke_reason"],
            metadata=metadata if isinstance(metadata, dict) else {},
        )

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.observe is None:
            return
        try:
            self.observe.emit(event_type, payload=payload)
        except Exception:
            logger.debug("capability grant emit failed: %s", event_type, exc_info=True)
