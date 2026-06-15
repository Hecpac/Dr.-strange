from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from claw_v2.approval_sensitivity import approval_metadata_for_change
from claw_v2.redaction import redact_sensitive
from claw_v2.turn_context import current_turn_id

logger = logging.getLogger(__name__)

APPROVAL_TTL_SECONDS = 900  # 15 minutes
VALID_APPROVAL_STATES = frozenset({"pending", "approved", "rejected", "expired", "archived"})
TERMINAL_APPROVAL_STATES = frozenset({"approved", "rejected", "expired", "archived"})

# AM-APPRSCAN (2026-06-12): list_pending runs on hot message paths; a short
# TTL bounds the directory rescan without serving stale approvals (every
# in-process mutation invalidates the cache immediately).
_LIST_PENDING_CACHE_TTL_SECONDS = 2.0

_NON_SENSITIVE_CONFIRMATIONS = frozenset(
    {
        "aprobada",
        "aprobado",
        "apruebalo",
        "apruebala",
        "aprobalo",
        "aprobala",
        "autorizado",
        "autorizada",
        "confirmado",
        "confirmada",
        "confirmo",
        "dale",
        "ok",
        "si",
        "sí",
        "yes",
        "approved",
    }
)

# Semantic axes — referenced by ApprovalManager.create defaults and by
# downstream consumers (telegram notifier, backpressure counter, audit).
DEFAULT_REQUESTED_BY = "unknown"
RESOLVED_BY_HUMAN = "human"
RESOLVED_BY_SYSTEM_AUTO = "system_auto"
RESOLVED_BY_EXPIRED = "expired"

# Default values applied when reading an approval JSON file that pre-dates
# the semantic-fields migration. Centralised so adding another axis only
# requires one update.
_SEMANTIC_DEFAULTS: dict[str, object] = {
    "risk_basis": None,
    "requested_by": DEFAULT_REQUESTED_BY,
    "visible_to_user": True,
    "resolved_by": None,
}


@dataclass(slots=True)
class PendingApproval:
    approval_id: str
    action: str
    summary: str
    token: str
    risk_code: str | None = None
    required_confirmation: str | None = None


class ApprovalManager:
    def __init__(
        self,
        root: Path | str,
        secret: str,
        *,
        ttl_seconds: int = APPROVAL_TTL_SECONDS,
        observe: Any | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # AM-APPRSCAN: (monotonic_ts, snapshot) — invalidated on every mutation.
        self._pending_cache: tuple[float, list[dict]] | None = None
        self.secret = secret.encode("utf-8")
        if int(ttl_seconds) <= 0:
            raise ValueError("approval ttl_seconds must be positive")
        self.ttl_seconds = int(ttl_seconds)
        self.observe = observe

    def create(
        self,
        action: str,
        summary: str,
        metadata: dict | None = None,
        *,
        risk_basis: str | None = None,
        requested_by: str | None = None,
        visible_to_user: bool = True,
        diff: str | None = None,
        changed_paths: Iterable[str] | None = None,
    ) -> PendingApproval:
        approval_id = secrets.token_hex(8)
        token = secrets.token_urlsafe(12)
        # P0-B: stamp the active turn_id on approval metadata so a single
        # turn_id query can pull message + tools + ledger + approval together.
        merged_metadata, sensitivity = approval_metadata_for_change(
            metadata=metadata,
            action=action,
            summary=summary,
            diff=diff,
            paths=changed_paths,
        )
        redacted_metadata = redact_sensitive(merged_metadata, limit=2000)
        active_turn_id = current_turn_id()
        if active_turn_id and "turn_id" not in redacted_metadata:
            redacted_metadata["turn_id"] = active_turn_id
        if risk_basis is None:
            risk_basis = sensitivity.risk_basis
        payload = {
            "approval_id": approval_id,
            "action": action,
            "summary": redact_sensitive(summary, limit=2000),
            "metadata": redacted_metadata,
            "token_hash": self._digest(token),
            "status": "pending",
            "created_at": time.time(),
            # P2 wave: semantic axes (R3 of the 2026-05-23 audit).
            "risk_basis": risk_basis,
            "requested_by": requested_by if requested_by is not None else DEFAULT_REQUESTED_BY,
            "visible_to_user": bool(visible_to_user),
            "resolved_by": None,
        }
        _atomic_write_json(self._path_for(approval_id), payload)
        self._pending_cache = None
        self._emit(
            "approval_created",
            {
                "approval_id": approval_id,
                "action": action,
                "status": "pending",
                "visible_to_user": payload["visible_to_user"],
            },
        )
        return PendingApproval(
            approval_id=approval_id,
            action=action,
            summary=str(payload["summary"]),
            token=token,
            risk_code=redacted_metadata.get("risk_code")
            if isinstance(redacted_metadata, dict)
            else None,
            required_confirmation=redacted_metadata.get("required_confirmation")
            if isinstance(redacted_metadata, dict)
            else None,
        )

    def approve(self, approval_id: str, token: str) -> bool:
        now = time.time()

        def _do_approve(payload: dict) -> None:
            # MED-2: single-use. Only a still-pending approval can be resolved
            # here; once approved/rejected/expired/archived the record is
            # immutable, so a replayed token cannot re-authorize the action
            # (no double-publish) and a wrong token cannot flip a resolved
            # record's status.
            if payload.get("status") != "pending":
                payload["_result"] = False
                return
            created = _coerce_timestamp(payload.get("created_at", 0))
            if now - created > self.ttl_seconds:
                payload["status"] = "expired"
                payload["resolved_by"] = RESOLVED_BY_EXPIRED
                payload["resolved_at"] = now
                payload["_result"] = False
                return
            required_confirmation = _required_confirmation(payload)
            if required_confirmation:
                # LOW (2026-06-12): compare bytes — compare_digest raises
                # TypeError on non-ASCII str (confirmations carry tildes/ñ).
                valid = hmac.compare_digest(
                    str(token).strip().encode("utf-8"), required_confirmation.encode("utf-8")
                )
            else:
                valid = hmac.compare_digest(payload["token_hash"], self._digest(token))
            payload["status"] = "approved" if valid else "rejected"
            payload["resolved_by"] = RESOLVED_BY_HUMAN
            payload["resolved_at"] = now
            payload["_result"] = valid

        result = self._locked_update(approval_id, _do_approve)
        approved = result.pop("_result", False)
        self._emit_resolution_event(result, resolved_at=now)
        return approved

    def approve_confirmation(self, approval_id: str, confirmation: str) -> bool:
        payload = self.read(approval_id)
        required_confirmation = _required_confirmation(payload)
        if required_confirmation:
            if not hmac.compare_digest(
                str(confirmation).strip().encode("utf-8"), required_confirmation.encode("utf-8")
            ):
                return False
            return self._approve_human_without_token(approval_id)
        if _normalize_confirmation(confirmation) not in _NON_SENSITIVE_CONFIRMATIONS:
            return False
        return self._approve_human_without_token(approval_id)

    def _approve_human_without_token(self, approval_id: str) -> bool:
        now = time.time()

        def _do_approve(payload: dict) -> None:
            if payload.get("status") != "pending":
                payload["_result"] = False
                return
            created = _coerce_timestamp(payload.get("created_at", 0))
            if now - created > self.ttl_seconds:
                payload["status"] = "expired"
                payload["resolved_by"] = RESOLVED_BY_EXPIRED
                payload["resolved_at"] = now
                payload["_result"] = False
                return
            payload["status"] = "approved"
            payload["resolved_by"] = RESOLVED_BY_HUMAN
            payload["resolved_at"] = now
            payload["_result"] = True

        result = self._locked_update(approval_id, _do_approve)
        approved = result.pop("_result", False)
        self._emit_resolution_event(result, resolved_at=now)
        return approved

    def approve_internal(self, approval_id: str) -> bool:
        now = time.time()

        def _do_approve(payload: dict) -> None:
            # MED-2 / #13: single-use. Only a still-pending record may be
            # auto-approved; a rejected/approved/expired/archived record must
            # not be resurrected within the TTL window.
            if payload.get("status") != "pending":
                payload["_result"] = False
                return
            created = _coerce_timestamp(payload.get("created_at", 0))
            if now - created > self.ttl_seconds:
                payload["status"] = "expired"
                payload["resolved_by"] = RESOLVED_BY_EXPIRED
                payload["resolved_at"] = now
                payload["_result"] = False
                return
            payload["status"] = "approved"
            payload["resolved_by"] = RESOLVED_BY_SYSTEM_AUTO
            payload["resolved_at"] = now
            payload["_result"] = True

        result = self._locked_update(approval_id, _do_approve)
        approved = result.pop("_result", False)
        self._emit_resolution_event(result, resolved_at=now)
        return approved

    def reject(self, approval_id: str) -> bool:
        now = time.time()

        def _do_reject(payload: dict) -> None:
            if payload.get("status") in TERMINAL_APPROVAL_STATES:
                payload["_result"] = False
                return
            payload["status"] = "rejected"
            payload["resolved_by"] = RESOLVED_BY_HUMAN
            payload["resolved_at"] = now
            payload["_result"] = True

        result = self._locked_update(approval_id, _do_reject)
        rejected = result.pop("_result", False)
        if rejected:
            self._emit("approval_rejected", _approval_event_payload(result))
        return rejected

    def archive(self, approval_id: str, *, reason: str = "") -> bool:
        def _do_archive(payload: dict) -> None:
            if payload.get("status") != "pending":
                payload["_result"] = False
                return
            payload["status"] = "archived"
            payload["archived_at"] = time.time()
            payload["resolved_by"] = RESOLVED_BY_HUMAN
            payload["resolved_at"] = time.time()
            if reason:
                payload["archive_reason"] = reason
            payload["_result"] = True

        result = self._locked_update(approval_id, _do_archive)
        archived = result.pop("_result", False)
        if archived:
            self._emit("approval_archived", _approval_event_payload(result))
        return archived

    def expire_due(self, now: datetime | float | int | None = None) -> int:
        now_ts = _coerce_timestamp(now)
        expired_count = 0
        inspected_count = 0
        skipped_count = 0
        for path in sorted(self.root.glob("*.json")):
            inspected_count += 1
            approval_id = path.stem

            def _expire_if_due(payload: dict) -> None:
                if payload.get("status") != "pending":
                    payload["_result"] = False
                    return
                created = _coerce_timestamp(payload.get("created_at", 0))
                if now_ts - created <= self.ttl_seconds:
                    payload["_result"] = False
                    return
                payload["status"] = "expired"
                payload["resolved_by"] = RESOLVED_BY_EXPIRED
                payload["resolved_at"] = now_ts
                payload["_result"] = True

            try:
                result = self._locked_update(approval_id, _expire_if_due)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
                skipped_count += 1
                continue
            expired = result.pop("_result", False)
            if expired:
                expired_count += 1
                self._emit("approval_expired", _approval_event_payload(result))
        self._emit(
            "approval_sweep_completed",
            {
                "expired_count": expired_count,
                "inspected_count": inspected_count,
                "skipped_count": skipped_count,
                "ttl_seconds": self.ttl_seconds,
            },
        )
        return expired_count

    def _locked_update(self, approval_id: str, modifier: object) -> dict:
        path = self._path_for(approval_id)
        while True:
            fd = os.open(str(path), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                # Updates persist via atomic replace, so the path may point to
                # a newer inode by the time the lock is acquired. Holding a
                # lock on an orphaned inode would lose this update — reopen.
                try:
                    if os.fstat(fd).st_ino != os.stat(str(path)).st_ino:
                        continue
                except FileNotFoundError:
                    continue
                raw = self._read_locked_fd(fd)
                payload = json.loads(raw.decode("utf-8"))
                modifier(payload)  # type: ignore[operator]
                # ``_result`` is a return side-channel, not record state — strip it
                # before persisting so a resolved record is content-immutable on
                # replay (and re-attach it for the caller).
                result = payload.pop("_result", False)
                # AH1 (2026-06-11): sign only resolutions made by this manager
                # in this update (`result` is True). A record forged on disk as
                # "approved" never gets a valid signature — and a no-op pass
                # over it must not stamp one retroactively.
                if result and payload.get("status") == "approved":
                    payload["resolution_sig"] = self._resolution_sig(payload)
                # Crash-safe persist: an in-place truncate+write left
                # permanently corrupt JSON if the process died mid-write,
                # and one corrupt record breaks the whole approval inbox.
                _atomic_write_json(path, payload)
                self._pending_cache = None
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            payload["_result"] = result
            return payload

    def status(self, approval_id: str) -> str:
        return self.read(approval_id)["status"]

    def read(self, approval_id: str) -> dict:
        path = self._path_for(approval_id)
        while True:
            fd = os.open(str(path), os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_SH)
                # Writers persist via atomic replace: if this fd points to an
                # inode that was swapped out while we waited for the lock, the
                # content is stale (e.g. still "pending" after a resolve) —
                # reopen and read the current record instead.
                try:
                    if os.fstat(fd).st_ino != os.stat(str(path)).st_ino:
                        continue
                except FileNotFoundError:
                    continue
                raw = self._read_locked_fd(fd)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            break
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"approval record {approval_id} is not a JSON object")
        # Backwards-compat: approvals persisted before the semantic-fields
        # migration must still surface every axis to callers.
        for key, default in _SEMANTIC_DEFAULTS.items():
            payload.setdefault(key, default)
        return payload

    def list_pending(self) -> list[dict]:
        # AM-APPRSCAN (2026-06-12): this scans and parses every record file
        # and runs on hot message paths. A short TTL cache (invalidated by
        # every mutation in _locked_update/create) bounds the per-message
        # cost; the reaper below keeps the directory itself small.
        now = time.monotonic()
        cached = self._pending_cache
        if cached is not None and now - cached[0] <= _LIST_PENDING_CACHE_TTL_SECONDS:
            return [dict(item) for item in cached[1]]
        pending: list[dict] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                payload = self.read(path.stem)
            except (json.JSONDecodeError, ValueError, OSError):
                # One unreadable record (truncated, or valid JSON that is not
                # an object) must not break the whole inbox (listing,
                # approving, and the ApprovalPending resume flow).
                logger.warning("skipping unreadable approval record %s", path, exc_info=True)
                continue
            if payload.get("status") == "pending":
                pending.append(payload)
        self._pending_cache = (now, [dict(item) for item in pending])
        return pending

    def reap_resolved(self, *, older_than_seconds: float = 7 * 86_400.0) -> int:
        """Move long-resolved records into ``root/resolved/`` so the hot
        ``list_pending`` glob stays small. Terminal records are immutable;
        relocation preserves them for audit without rescanning them on
        every message (AM-APPRSCAN, 2026-06-12)."""
        cutoff = time.time() - max(0.0, older_than_seconds)
        archive_dir = self.root / "resolved"
        moved = 0
        for path in sorted(self.root.glob("*.json")):
            try:
                payload = self.read(path.stem)
            except (json.JSONDecodeError, ValueError, OSError):
                continue
            if payload.get("status") not in TERMINAL_APPROVAL_STATES:
                continue
            resolved_at = _coerce_timestamp(
                payload.get("resolved_at")
                or payload.get("archived_at")
                or payload.get("created_at")
                or 0
            )
            if resolved_at > cutoff:
                continue
            try:
                archive_dir.mkdir(parents=True, exist_ok=True)
                path.rename(archive_dir / path.name)
                moved += 1
            except OSError:
                logger.debug("approval reap failed for %s", path, exc_info=True)
        if moved:
            self._pending_cache = None
            self._emit("approval_reap_completed", {"moved_count": moved})
        return moved

    def list_pending_visible_to_user(self) -> list[dict]:
        """Subset of ``list_pending()`` restricted to approvals that should
        be surfaced in Telegram. Daemon-internal kairos approvals can opt
        out via ``visible_to_user=False`` so the user-facing inbox stays
        focused on the ones that genuinely need a human signal.
        """
        return [p for p in self.list_pending() if p.get("visible_to_user", True)]

    def _resolution_sig(self, payload: dict) -> str:
        basis = "|".join(
            (
                str(payload.get("approval_id") or ""),
                str(payload.get("status") or ""),
                str(payload.get("resolved_by") or ""),
                str(payload.get("resolved_at") or ""),
            )
        )
        return hmac.new(self.secret, basis.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify_resolution(self, payload: dict) -> bool:
        """True only for an "approved" record whose resolution was stamped by
        this manager's secret — a file rewritten on disk cannot produce it."""
        if payload.get("status") != "approved":
            return False
        sig = str(payload.get("resolution_sig") or "")
        if not sig:
            return False
        return hmac.compare_digest(sig, self._resolution_sig(payload))

    def _path_for(self, approval_id: str) -> Path:
        return self.root / f"{approval_id}.json"

    def _digest(self, token: str) -> str:
        return hmac.new(self.secret, token.encode("utf-8"), hashlib.sha256).hexdigest()

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.observe is None:
            return
        try:
            self.observe.emit(event_type, payload=redact_sensitive(payload, limit=2000))
        except Exception:
            pass

    def _emit_resolution_event(self, payload: dict[str, Any], *, resolved_at: float) -> None:
        if payload.get("resolved_at") != resolved_at:
            return
        event_by_status = {
            "approved": "approval_approved",
            "rejected": "approval_rejected",
            "expired": "approval_expired",
        }
        event_type = event_by_status.get(str(payload.get("status")))
        if event_type:
            self._emit(event_type, _approval_event_payload(payload))

    @staticmethod
    def _read_locked_fd(fd: int) -> bytes:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Persist an approval record via unique tmp file + atomic rename.

    Readers can never observe a partially written record, and a crash
    mid-write leaves the previous version intact. Mode 0600 and a leading
    dot keep tmp files out of the ``*.json`` listing glob.
    """
    data = json.dumps(payload, indent=2).encode("utf-8")
    tmp = path.parent / f".{path.name}.{secrets.token_hex(4)}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)
    os.replace(tmp, path)


def _required_confirmation(payload: dict) -> str | None:
    metadata = payload.get("metadata") or {}
    required = metadata.get("required_confirmation") if isinstance(metadata, dict) else None
    if not required:
        return None
    return str(required).strip()


def _normalize_confirmation(value: str) -> str:
    return " ".join(str(value).strip().lower().split())


def _coerce_timestamp(value: datetime | float | int | object | None) -> float:
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _approval_event_payload(payload: dict) -> dict[str, Any]:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return {
        "approval_id": payload.get("approval_id"),
        "action": payload.get("action"),
        "status": payload.get("status"),
        "resolved_by": payload.get("resolved_by"),
        "visible_to_user": payload.get("visible_to_user", True),
        "risk_code": metadata.get("risk_code") if isinstance(metadata, dict) else None,
    }
