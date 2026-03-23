from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from pathlib import Path


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
        payload = {
            "approval_id": approval_id,
            "action": action,
            "summary": summary,
            "metadata": metadata or {},
            "token_hash": self._digest(token),
            "status": "pending",
        }
        self._path_for(approval_id).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return PendingApproval(approval_id=approval_id, action=action, summary=summary, token=token)

    def approve(self, approval_id: str, token: str) -> bool:
        path = self._path_for(approval_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        valid = hmac.compare_digest(payload["token_hash"], self._digest(token))
        payload["status"] = "approved" if valid else "rejected"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return valid

    def status(self, approval_id: str) -> str:
        return json.loads(self._path_for(approval_id).read_text(encoding="utf-8"))["status"]

    def read(self, approval_id: str) -> dict:
        return json.loads(self._path_for(approval_id).read_text(encoding="utf-8"))

    def list_pending(self) -> list[dict]:
        pending: list[dict] = []
        for path in sorted(self.root.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("status") == "pending":
                pending.append(payload)
        return pending

    def _path_for(self, approval_id: str) -> Path:
        return self.root / f"{approval_id}.json"

    def _digest(self, token: str) -> str:
        return hmac.new(self.secret, token.encode("utf-8"), hashlib.sha256).hexdigest()
