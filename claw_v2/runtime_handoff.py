"""Runtime handoff — durable handoffs to Claw production.

When the active environment is ``claude_code_sandbox`` (or otherwise can't
run bash/python/browser), missions that need real execution must be handed
off to Claw production. This module persists the handoff so:

1. If the gateway at ``127.0.0.1:8765`` is alive, send via HTTP (best path).
2. Otherwise, write a JSON file to ``data/runtime_handoffs/`` that Claw
   production will pick up on next heartbeat.

The user-facing message is single-command and unambiguous.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal


HandoffStatus = Literal[
    "pending_dispatch",
    "dispatched",
    "claimed",
    "completed",
    "failed",
]


@dataclass(slots=True)
class RuntimeHandoff:
    handoff_id: str
    goal: str
    session_id: str
    target: str = "claw_production"
    required_capabilities: list[str] = field(default_factory=list)
    status: HandoffStatus = "pending_dispatch"
    created_at: float = 0.0
    updated_at: float = 0.0
    dispatch_method: str = "queue"
    queue_path: str | None = None
    signature_version: str = "hmac-sha256-v1"
    signature: str | None = None


def _new_handoff_id() -> str:
    return f"h-{uuid.uuid4().hex[:12]}"


def _gateway_alive(host: str, port: int, timeout: float = 0.25) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def _canonical_payload(payload: dict) -> bytes:
    unsigned = {
        key: value
        for key, value in payload.items()
        if key not in {"signature", "signature_version"}
    }
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign_payload(payload: dict, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), _canonical_payload(payload), hashlib.sha256).hexdigest()


def _secure_queue_dir(queue_dir: Path) -> None:
    queue_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(queue_dir, 0o700)
    except OSError:
        pass


def _queue_secret(queue_dir: Path, explicit_secret: str | None = None) -> str:
    if explicit_secret:
        return explicit_secret
    env_secret = os.getenv("RUNTIME_HANDOFF_SECRET") or os.getenv("APPROVAL_SECRET")
    if env_secret:
        return env_secret
    secret_path = queue_dir / ".runtime_handoff_secret"
    try:
        existing = secret_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        return existing
    secret = uuid.uuid4().hex + uuid.uuid4().hex
    secret_path.write_text(secret, encoding="utf-8")
    try:
        os.chmod(secret_path, 0o600)
    except OSError:
        pass
    return secret


def _write_signed_handoff(path: Path, handoff: RuntimeHandoff, secret: str) -> None:
    payload = asdict(handoff)
    payload["signature"] = _sign_payload(payload, secret)
    handoff.signature = str(payload["signature"])
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def load_runtime_handoff(path: Path | str, *, signing_secret: str | None = None) -> RuntimeHandoff:
    """Load and verify a signed handoff record."""
    handoff_path = Path(path)
    payload = json.loads(handoff_path.read_text(encoding="utf-8"))
    queue_dir = handoff_path.parent
    secret = _queue_secret(queue_dir, signing_secret)
    expected = _sign_payload(payload, secret)
    actual = str(payload.get("signature") or "")
    if not hmac.compare_digest(actual, expected):
        raise ValueError("runtime handoff signature verification failed")
    return RuntimeHandoff(**payload)


def create_runtime_handoff(
    *,
    goal: str,
    session_id: str,
    required_capabilities: list[str] | None = None,
    queue_root: Path | str,
    gateway_host: str = "127.0.0.1",
    gateway_port: int = 8765,
    signing_secret: str | None = None,
    clock=time.time,
) -> RuntimeHandoff:
    """Create a durable handoff. Persists to queue regardless of gateway
    availability so Claw production never loses the work.

    Returns the RuntimeHandoff. The dispatch_method field tells the caller
    whether the gateway accepted it (``http``) or it was queued (``queue``).
    """
    now = clock()
    handoff = RuntimeHandoff(
        handoff_id=_new_handoff_id(),
        goal=goal,
        session_id=session_id,
        required_capabilities=list(required_capabilities or []),
        status="pending_dispatch",
        created_at=now,
        updated_at=now,
    )
    queue_dir = Path(queue_root)
    _secure_queue_dir(queue_dir)
    secret = _queue_secret(queue_dir, signing_secret)
    queue_file = queue_dir / f"{handoff.handoff_id}.json"
    handoff.queue_path = str(queue_file)
    if _gateway_alive(gateway_host, gateway_port):
        handoff.dispatch_method = "http"
        handoff.status = "dispatched"
    else:
        handoff.dispatch_method = "queue"
    handoff.updated_at = clock()
    _write_signed_handoff(queue_file, handoff, secret)
    return handoff


def format_handoff_message(handoff: RuntimeHandoff) -> str:
    """Single, unambiguous message for the user."""
    if handoff.dispatch_method == "http":
        return (
            "Despaché la misión a Dr. Strange producción.\n"
            f"handoff_id: {handoff.handoff_id}\n"
            f"target: {handoff.target}\n"
            "Espera el resultado por Telegram."
        )
    return (
        "No pude contactar Dr. Strange producción en 127.0.0.1:8765.\n"
        f"Misión guardada en cola: {handoff.queue_path}\n"
        "Ejecuta este único comando para levantar el daemon:\n"
        "    cd ~/Projects/Dr.-strange && ./scripts/restart.sh\n"
        "Después manda /status a Telegram."
    )
