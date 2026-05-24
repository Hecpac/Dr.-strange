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

from claw_v2.turn_context import current_turn_id

APPROVAL_TTL_SECONDS = 900  # 15 minutes


@dataclass(slots=True)
class PendingApproval:
    approval_id: str
    action: str
    summary: str
    token: str


class ApprovalManager:
    def __init__(self, root: Path | str, secret: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.secret = secret.encode("utf-8")

    def create(self, action: str, summary: str, metadata: dict | None = None) -> PendingApproval:
        approval_id = secrets.token_hex(8)
        token = secrets.token_urlsafe(12)
        # P0-B: stamp the active turn_id on approval metadata so a single
        # turn_id query can pull message + tools + ledger + approval together.
        merged_metadata = dict(metadata or {})
        active_turn_id = current_turn_id()
        if active_turn_id and "turn_id" not in merged_metadata:
            merged_metadata["turn_id"] = active_turn_id
        payload = {
            "approval_id": approval_id,
            "action": action,
            "summary": summary,
            "metadata": merged_metadata,
            "token_hash": self._digest(token),
            "status": "pending",
            "created_at": time.time(),
        }
        self._path_for(approval_id).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return PendingApproval(approval_id=approval_id, action=action, summary=summary, token=token)

    def approve(self, approval_id: str, token: str) -> bool:
        def _do_approve(payload: dict) -> None:
            created = payload.get("created_at", 0)
            if time.time() - created > APPROVAL_TTL_SECONDS:
                payload["status"] = "expired"
                payload["_result"] = False
                return
            valid = hmac.compare_digest(payload["token_hash"], self._digest(token))
            payload["status"] = "approved" if valid else "rejected"
            payload["_result"] = valid
        result = self._locked_update(approval_id, _do_approve)
        return result.pop("_result", False)

    def approve_internal(self, approval_id: str) -> bool:
        def _do_approve(payload: dict) -> None:
            created = payload.get("created_at", 0)
            if time.time() - created > APPROVAL_TTL_SECONDS:
                payload["status"] = "expired"
                payload["_result"] = False
                return
            payload["status"] = "approved"
            payload["_result"] = True

        result = self._locked_update(approval_id, _do_approve)
        return result.pop("_result", False)

    def reject(self, approval_id: str) -> None:
        self._locked_update(approval_id, lambda p: p.__setitem__("status", "rejected"))

    def archive(self, approval_id: str, *, reason: str = "") -> bool:
        def _do_archive(payload: dict) -> None:
            if payload.get("status") != "pending":
                payload["_result"] = False
                return
            payload["status"] = "archived"
            payload["archived_at"] = time.time()
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
            new_data = json.dumps(payload, indent=2).encode("utf-8")
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, new_data)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
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
        return json.loads(raw.decode("utf-8"))

    def list_pending(self) -> list[dict]:
        pending: list[dict] = []
        for path in sorted(self.root.glob("*.json")):
            payload = self.read(path.stem)
            if payload.get("status") == "pending":
                pending.append(payload)
        return pending

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
