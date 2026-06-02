from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from claw_v2.approval_sensitivity import approval_metadata_for_change
from claw_v2.turn_context import current_turn_id

APPROVAL_TTL_SECONDS = 900  # 15 minutes

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
    def __init__(self, root: Path | str, secret: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.secret = secret.encode("utf-8")

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
        active_turn_id = current_turn_id()
        if active_turn_id and "turn_id" not in merged_metadata:
            merged_metadata["turn_id"] = active_turn_id
        if risk_basis is None:
            risk_basis = sensitivity.risk_basis
        payload = {
            "approval_id": approval_id,
            "action": action,
            "summary": summary,
            "metadata": merged_metadata,
            "token_hash": self._digest(token),
            "status": "pending",
            "created_at": time.time(),
            # P2 wave: semantic axes (R3 of the 2026-05-23 audit).
            "risk_basis": risk_basis,
            "requested_by": requested_by if requested_by is not None else DEFAULT_REQUESTED_BY,
            "visible_to_user": bool(visible_to_user),
            "resolved_by": None,
        }
        self._path_for(approval_id).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return PendingApproval(
            approval_id=approval_id,
            action=action,
            summary=summary,
            token=token,
            risk_code=merged_metadata.get("risk_code"),
            required_confirmation=merged_metadata.get("required_confirmation"),
        )

    def approve(self, approval_id: str, token: str) -> bool:
        def _do_approve(payload: dict) -> None:
            # MED-2: single-use. Only a still-pending approval can be resolved
            # here; once approved/rejected/expired/archived the record is
            # immutable, so a replayed token cannot re-authorize the action
            # (no double-publish) and a wrong token cannot flip a resolved
            # record's status.
            if payload.get("status") != "pending":
                payload["_result"] = False
                return
            created = payload.get("created_at", 0)
            if time.time() - created > APPROVAL_TTL_SECONDS:
                payload["status"] = "expired"
                payload["resolved_by"] = RESOLVED_BY_EXPIRED
                payload["resolved_at"] = time.time()
                payload["_result"] = False
                return
            required_confirmation = _required_confirmation(payload)
            if required_confirmation:
                valid = hmac.compare_digest(str(token).strip(), required_confirmation)
            else:
                valid = hmac.compare_digest(payload["token_hash"], self._digest(token))
            payload["status"] = "approved" if valid else "rejected"
            payload["resolved_by"] = RESOLVED_BY_HUMAN
            payload["resolved_at"] = time.time()
            payload["_result"] = valid
        result = self._locked_update(approval_id, _do_approve)
        return result.pop("_result", False)

    def approve_confirmation(self, approval_id: str, confirmation: str) -> bool:
        payload = self.read(approval_id)
        required_confirmation = _required_confirmation(payload)
        if required_confirmation:
            if not hmac.compare_digest(str(confirmation).strip(), required_confirmation):
                return False
            return self._approve_human_without_token(approval_id)
        if _normalize_confirmation(confirmation) not in _NON_SENSITIVE_CONFIRMATIONS:
            return False
        return self._approve_human_without_token(approval_id)

    def _approve_human_without_token(self, approval_id: str) -> bool:
        def _do_approve(payload: dict) -> None:
            if payload.get("status") != "pending":
                payload["_result"] = False
                return
            created = payload.get("created_at", 0)
            if time.time() - created > APPROVAL_TTL_SECONDS:
                payload["status"] = "expired"
                payload["resolved_by"] = RESOLVED_BY_EXPIRED
                payload["resolved_at"] = time.time()
                payload["_result"] = False
                return
            payload["status"] = "approved"
            payload["resolved_by"] = RESOLVED_BY_HUMAN
            payload["resolved_at"] = time.time()
            payload["_result"] = True

        result = self._locked_update(approval_id, _do_approve)
        return result.pop("_result", False)

    def approve_internal(self, approval_id: str) -> bool:
        def _do_approve(payload: dict) -> None:
            created = payload.get("created_at", 0)
            if time.time() - created > APPROVAL_TTL_SECONDS:
                payload["status"] = "expired"
                payload["resolved_by"] = RESOLVED_BY_EXPIRED
                payload["resolved_at"] = time.time()
                payload["_result"] = False
                return
            payload["status"] = "approved"
            payload["resolved_by"] = RESOLVED_BY_SYSTEM_AUTO
            payload["resolved_at"] = time.time()
            payload["_result"] = True

        result = self._locked_update(approval_id, _do_approve)
        return result.pop("_result", False)

    def reject(self, approval_id: str) -> None:
        def _do_reject(payload: dict) -> None:
            payload["status"] = "rejected"
            payload["resolved_by"] = RESOLVED_BY_HUMAN
            payload["resolved_at"] = time.time()

        self._locked_update(approval_id, _do_reject)

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
        return result.pop("_result", False)

    def _locked_update(self, approval_id: str, modifier: object) -> dict:
        path = self._path_for(approval_id)
        fd = os.open(str(path), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = self._read_locked_fd(fd)
            payload = json.loads(raw.decode("utf-8"))
            modifier(payload)  # type: ignore[operator]
            # ``_result`` is a return side-channel, not record state — strip it
            # before persisting so a resolved record is content-immutable on
            # replay (and re-attach it for the caller).
            result = payload.pop("_result", False)
            new_data = json.dumps(payload, indent=2).encode("utf-8")
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, new_data)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        payload["_result"] = result
        return payload

    def status(self, approval_id: str) -> str:
        return self.read(approval_id)["status"]

    def read(self, approval_id: str) -> dict:
        path = self._path_for(approval_id)
        fd = os.open(str(path), os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            raw = self._read_locked_fd(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        payload = json.loads(raw.decode("utf-8"))
        # Backwards-compat: approvals persisted before the semantic-fields
        # migration must still surface every axis to callers.
        for key, default in _SEMANTIC_DEFAULTS.items():
            payload.setdefault(key, default)
        return payload

    def list_pending(self) -> list[dict]:
        pending: list[dict] = []
        for path in sorted(self.root.glob("*.json")):
            payload = self.read(path.stem)
            if payload.get("status") == "pending":
                pending.append(payload)
        return pending

    def list_pending_visible_to_user(self) -> list[dict]:
        """Subset of ``list_pending()`` restricted to approvals that should
        be surfaced in Telegram. Daemon-internal kairos approvals can opt
        out via ``visible_to_user=False`` so the user-facing inbox stays
        focused on the ones that genuinely need a human signal.
        """
        return [p for p in self.list_pending() if p.get("visible_to_user", True)]

    def _path_for(self, approval_id: str) -> Path:
        return self.root / f"{approval_id}.json"

    def _digest(self, token: str) -> str:
        return hmac.new(self.secret, token.encode("utf-8"), hashlib.sha256).hexdigest()

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


def _required_confirmation(payload: dict) -> str | None:
    metadata = payload.get("metadata") or {}
    required = metadata.get("required_confirmation") if isinstance(metadata, dict) else None
    if not required:
        return None
    return str(required).strip()


def _normalize_confirmation(value: str) -> str:
    return " ".join(str(value).strip().lower().split())
