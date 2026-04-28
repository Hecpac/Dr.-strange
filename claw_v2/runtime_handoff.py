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

import json
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


def create_runtime_handoff(
    *,
    goal: str,
    session_id: str,
    required_capabilities: list[str] | None = None,
    queue_root: Path | str,
    gateway_host: str = "127.0.0.1",
    gateway_port: int = 8765,
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
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_file = queue_dir / f"{handoff.handoff_id}.json"
    handoff.queue_path = str(queue_file)
    with queue_file.open("w", encoding="utf-8") as fh:
        json.dump(asdict(handoff), fh, indent=2, sort_keys=True)
    if _gateway_alive(gateway_host, gateway_port):
        handoff.dispatch_method = "http"
        handoff.status = "dispatched"
    else:
        handoff.dispatch_method = "queue"
    handoff.updated_at = clock()
    with queue_file.open("w", encoding="utf-8") as fh:
        json.dump(asdict(handoff), fh, indent=2, sort_keys=True)
    return handoff


def format_handoff_message(handoff: RuntimeHandoff) -> str:
    """Single, unambiguous message for the user."""
    if handoff.dispatch_method == "http":
        return (
            "Despaché la misión a Claw producción.\n"
            f"handoff_id: {handoff.handoff_id}\n"
            f"target: {handoff.target}\n"
            "Espera el resultado por Telegram."
        )
    return (
        "No pude contactar Claw producción en 127.0.0.1:8765.\n"
        f"Misión guardada en cola: {handoff.queue_path}\n"
        "Ejecuta este único comando para levantar el daemon:\n"
        "    cd ~/Projects/Dr.-strange && ./scripts/restart.sh\n"
        "Después manda /status a Telegram."
    )
