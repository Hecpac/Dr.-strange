"""Agent2Agent (A2A) Protocol — basic support for inter-agent communication.

Implements a lightweight A2A endpoint so Claw can:
1. Advertise its capabilities via an Agent Card
2. Receive tasks from external agents
3. Send tasks to other A2A-compatible agents

Based on the open A2A protocol (Google, 50+ partners).
Transport: HTTP + JSON-RPC + SSE.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claw_v2.llm import LLMRouter

logger = logging.getLogger(__name__)

_DEFAULT_A2A_ROOT = Path.home() / ".claw" / "a2a"


@dataclass(slots=True)
class AgentCard:
    """Agent Card — public identity and capabilities advertisement."""
    name: str = "Claw"
    description: str = "Autonomous AI assistant by Pachano Design"
    version: str = "2.0"
    capabilities: list[str] = field(default_factory=lambda: [
        "wiki_search", "web_research", "code_analysis", "site_monitoring",
        "social_publishing", "task_execution",
    ])
    protocols: list[str] = field(default_factory=lambda: ["a2a/1.0", "mcp/1.0"])
    endpoint: str = ""
    owner: str = "Pachano Design"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class A2ATask:
    """A task received from or sent to another agent."""
    id: str
    from_agent: str
    to_agent: str
    action: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # pending, accepted, in_progress, completed, failed, rejected
    result: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    completed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class A2AService:
    """Manages A2A protocol interactions."""

    def __init__(self, *, router: LLMRouter | None = None, root: Path | None = None) -> None:
        self.router = router
        self.root = root or _DEFAULT_A2A_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._card = AgentCard()
        self._peers: dict[str, AgentCard] = {}
        self._inbox: list[A2ATask] = []
        self._outbox: list[A2ATask] = []
        self._a2a_secret = os.getenv("A2A_SECRET", "")
        self._load_peers()
        self._load_inbox()

    def _peers_path(self) -> Path:
        return self.root / "peers.json"

    def _inbox_path(self) -> Path:
        return self.root / "inbox.json"

    def _load_peers(self) -> None:
        path = self._peers_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for name, peer in data.items():
                self._peers[name] = AgentCard(
                    name=peer.get("name", name),
                    description=peer.get("description", ""),
                    version=peer.get("version", "1.0"),
                    capabilities=peer.get("capabilities", []),
                    protocols=peer.get("protocols", []),
                    endpoint=peer.get("endpoint", ""),
                    owner=peer.get("owner", ""),
                )
        except Exception:
            logger.exception("Failed to load A2A peers")

    def _save_peers(self) -> None:
        data = {name: card.to_dict() for name, card in self._peers.items()}
        self._peers_path().write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _save_inbox(self) -> None:
        data = [t.to_dict() for t in self._inbox[-50:]]  # keep last 50
        self._inbox_path().write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _load_inbox(self) -> None:
        path = self._inbox_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._inbox = [
                A2ATask(
                    id=entry.get("id", str(uuid.uuid4())),
                    from_agent=entry.get("from_agent", "unknown"),
                    to_agent=entry.get("to_agent", self._card.name),
                    action=entry.get("action", ""),
                    payload=entry.get("payload", {}),
                    status=entry.get("status", "pending"),
                    result=entry.get("result", {}),
                    created_at=entry.get("created_at", ""),
                    completed_at=entry.get("completed_at", ""),
                )
                for entry in data
            ]
        except Exception:
            logger.exception("Failed to load A2A inbox")

    # --- Agent Card ---

    def get_card(self) -> dict[str, Any]:
        """Return this agent's card for advertisement."""
        return self._card.to_dict()

    def set_endpoint(self, endpoint: str) -> None:
        self._card.endpoint = endpoint

    # --- Peer Management ---

    def register_peer(self, name: str, endpoint: str, capabilities: list[str] | None = None) -> dict:
        """Register an external agent as a known peer."""
        with self._lock:
            self._peers[name] = AgentCard(
                name=name,
                endpoint=endpoint,
                capabilities=capabilities or [],
            )
            self._save_peers()
        logger.info("A2A peer registered: %s at %s", name, endpoint)
        return {"registered": name, "endpoint": endpoint}

    def list_peers(self) -> list[dict[str, Any]]:
        return [{"name": c.name, "endpoint": c.endpoint, "capabilities": c.capabilities}
                for c in self._peers.values()]

    # --- Task Exchange ---

    def send_task(self, *, to_agent: str, action: str, payload: dict | None = None) -> dict:
        """Send a task to a peer agent via A2A protocol."""
        peer = self._peers.get(to_agent)
        if not peer:
            return {"success": False, "error": f"Unknown peer: {to_agent}"}
        if not peer.endpoint:
            return {"success": False, "error": f"Peer {to_agent} has no endpoint configured"}

        task = A2ATask(
            id=str(uuid.uuid4()),
            from_agent=self._card.name,
            to_agent=to_agent,
            action=action,
            payload=payload or {},
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        # HTTP POST to peer's A2A endpoint
        try:
            import httpx
            resp = httpx.post(
                f"{peer.endpoint}/a2a/tasks",
                json={
                    "jsonrpc": "2.0",
                    "method": "tasks/send",
                    "id": task.id,
                    "params": task.to_dict(),
                },
                timeout=30,
            )
            if resp.status_code < 400:
                task.status = "accepted"
                self._outbox.append(task)
                return {"success": True, "task_id": task.id, "status": "accepted"}
            else:
                return {"success": False, "error": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def verify_signature(self, body: bytes, signature: str) -> bool:
        if not self._a2a_secret:
            return False
        expected = hmac.new(self._a2a_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def receive_task(self, task_data: dict, *, signature: str = "") -> dict:
        """Process an incoming A2A task from another agent."""
        if self._a2a_secret and not self.verify_signature(
            json.dumps(task_data, sort_keys=True).encode(), signature
        ):
            logger.warning("A2A task rejected: invalid or missing HMAC signature")
            return {"task_id": "", "status": "rejected", "error": "authentication required"}
        task = A2ATask(
            id=task_data.get("id", str(uuid.uuid4())),
            from_agent=task_data.get("from_agent", "unknown"),
            to_agent=self._card.name,
            action=task_data.get("action", ""),
            payload=task_data.get("payload", {}),
            created_at=task_data.get("created_at", datetime.now(timezone.utc).isoformat()),
        )

        # Validate we can handle this action
        if task.action not in self._card.capabilities:
            task.status = "rejected"
            task.result = {"error": f"Unsupported action: {task.action}"}
        else:
            task.status = "accepted"

        with self._lock:
            self._inbox.append(task)
            self._save_inbox()

        logger.info("A2A task received: %s from %s (action=%s, status=%s)",
                     task.id, task.from_agent, task.action, task.status)
        return {"task_id": task.id, "status": task.status}

    def process_inbox(self) -> dict:
        """Process pending tasks in the inbox. Designed for cron/tick."""
        if not self.router:
            return {"processed": 0, "error": "No router"}

        processed = 0
        failed = 0
        mutated = False
        for task in self._inbox:
            if task.status != "accepted":
                continue
            task.status = "in_progress"
            mutated = True
            try:
                # Route to appropriate handler based on action
                result = self._execute_task(task)
                task.result = result
                task.status = "completed"
                task.completed_at = datetime.now(timezone.utc).isoformat()
                processed += 1
            except Exception as e:
                logger.exception("A2A task %s failed", task.id)
                task.status = "failed"
                task.result = {"error": str(e)}
                task.completed_at = datetime.now(timezone.utc).isoformat()
                failed += 1

        if mutated:
            with self._lock:
                self._save_inbox()

        return {
            "processed": processed,
            "failed": failed,
            "pending": sum(1 for t in self._inbox if t.status == "accepted"),
        }

    def _execute_task(self, task: A2ATask) -> dict:
        """Execute an A2A task using the LLM router."""
        prompt = (
            f"You are processing a task from agent '{task.from_agent}'.\n"
            f"Action: {task.action}\n"
            f"Payload: {json.dumps(task.payload)}\n\n"
            "Execute this task and return a concise result."
        )
        resp = self.router.ask(prompt, lane="worker", max_budget=0.20, timeout=60.0)
        return {"response": resp.content[:2000]}

    def stats(self) -> dict:
        return {
            "peers": len(self._peers),
            "inbox_total": len(self._inbox),
            "inbox_pending": sum(1 for t in self._inbox if t.status == "accepted"),
            "outbox_total": len(self._outbox),
        }
