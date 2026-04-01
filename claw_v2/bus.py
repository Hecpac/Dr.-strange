from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

KNOWN_AGENTS = ("hex", "rook", "alma", "lux")


@dataclass(slots=True)
class AgentMessage:
    id: str
    from_agent: str
    to_agent: str | None
    intent: Literal["notify", "request", "escalate", "reply"]
    topic: str
    payload: dict[str, Any]
    priority: Literal["low", "normal", "urgent"]
    ttl_seconds: int = 3600
    correlation_id: str = ""
    created_at: float = 0.0
    consumed_at: float | None = None


def _new_message(
    *,
    from_agent: str,
    to_agent: str | None,
    intent: Literal["notify", "request", "escalate", "reply"],
    topic: str,
    payload: dict[str, Any],
    priority: Literal["low", "normal", "urgent"] = "normal",
    ttl_seconds: int = 3600,
    correlation_id: str = "",
) -> AgentMessage:
    return AgentMessage(
        id=uuid.uuid4().hex,
        from_agent=from_agent,
        to_agent=to_agent,
        intent=intent,
        topic=topic,
        payload=payload,
        priority=priority,
        ttl_seconds=ttl_seconds,
        correlation_id=correlation_id or uuid.uuid4().hex,
        created_at=time.time(),
    )


class AgentBus:
    def __init__(self, bus_root: Path = Path.home() / ".claw" / "bus") -> None:
        self.bus_root = bus_root
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for agent in KNOWN_AGENTS:
            (self.bus_root / "inbox" / agent).mkdir(parents=True, exist_ok=True)
        (self.bus_root / "broadcast").mkdir(parents=True, exist_ok=True)
        (self.bus_root / "archive").mkdir(parents=True, exist_ok=True)

    def _write_message(self, path: Path, msg: AgentMessage) -> None:
        path.write_text(json.dumps(asdict(msg), ensure_ascii=False), encoding="utf-8")

    def _read_message(self, path: Path) -> AgentMessage:
        data = json.loads(path.read_text(encoding="utf-8"))
        return AgentMessage(**data)

    def send(self, message: AgentMessage) -> str:
        """Persist message to recipient inbox. Broadcasts use eager fan-out."""
        if message.to_agent is not None:
            self._write_message(
                self.bus_root / "inbox" / message.to_agent / f"{message.id}.json",
                message,
            )
        else:
            for agent in KNOWN_AGENTS:
                if agent != message.from_agent:
                    self._write_message(
                        self.bus_root / "inbox" / agent / f"{message.id}.json",
                        message,
                    )
            self._write_message(
                self.bus_root / "broadcast" / f"{message.id}.json",
                message,
            )
        if message.intent == "escalate":
            logger.info("BUS escalation from %s: %s", message.from_agent, message.topic)
        return message.id

    def receive(self, agent_name: str, *, max_messages: int = 20) -> list[AgentMessage]:
        """Consume messages from agent's inbox. Moves consumed to archive."""
        inbox = self.bus_root / "inbox" / agent_name
        now = time.time()
        messages: list[AgentMessage] = []
        for path in inbox.glob("*.json"):
            try:
                msg = self._read_message(path)
                if msg.created_at + msg.ttl_seconds < now:
                    path.unlink(missing_ok=True)
                    continue
                msg.consumed_at = now
                self._write_message(self.bus_root / "archive" / path.name, msg)
                path.unlink(missing_ok=True)
                messages.append(msg)
            except Exception:
                logger.warning("Failed to read bus message %s", path)
        messages.sort(key=lambda m: m.created_at, reverse=True)
        return messages[:max_messages]

    def reply(self, original: AgentMessage, *, content: dict, from_agent: str) -> str:
        """Send a reply linked to the original via correlation_id."""
        reply_msg = _new_message(
            from_agent=from_agent,
            to_agent=original.from_agent,
            intent="reply",
            topic=original.topic,
            payload=content,
            correlation_id=original.correlation_id,
        )
        return self.send(reply_msg)

    def pending_count(self, agent_name: str) -> int:
        """Count unconsumed messages in inbox."""
        inbox = self.bus_root / "inbox" / agent_name
        return len(list(inbox.glob("*.json")))

    def pending_urgent(self) -> list[AgentMessage]:
        """All unconsumed messages with priority=urgent across all inboxes."""
        urgent: list[AgentMessage] = []
        for agent in KNOWN_AGENTS:
            inbox = self.bus_root / "inbox" / agent
            for path in inbox.glob("*.json"):
                try:
                    msg = self._read_message(path)
                    if msg.priority == "urgent":
                        urgent.append(msg)
                except Exception:
                    pass
        return urgent

    def scan_expired_requests(self) -> list[AgentMessage]:
        """Scan all inboxes for intent=request messages past TTL without a reply."""
        now = time.time()
        expired: list[AgentMessage] = []
        for agent in KNOWN_AGENTS:
            inbox = self.bus_root / "inbox" / agent
            for path in inbox.glob("*.json"):
                try:
                    msg = self._read_message(path)
                    if msg.intent == "request" and msg.created_at + msg.ttl_seconds < now:
                        expired.append(msg)
                except Exception:
                    pass
        return expired

    def cleanup(self, max_age_days: int = 7) -> int:
        """Remove archived messages older than max_age_days."""
        archive = self.bus_root / "archive"
        cutoff = time.time() - (max_age_days * 86400)
        removed = 0
        for path in archive.glob("*.json"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        return removed
