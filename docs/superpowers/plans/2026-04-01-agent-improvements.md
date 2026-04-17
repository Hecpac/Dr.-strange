# Agent System Improvements — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three structural gaps in the Claw v2 agent ecosystem: add inter-agent communication bus, make Kairos executable, balance skills across agents, and add cross-agent memory consolidation with ecosystem health metrics.

**Architecture:** Three layers built in dependency order. Layer 1 (bus, kairos, registry) is pure infrastructure with no SKILL.md changes. Layer 2 (9 new SKILL.md files) is content-only with no Python changes. Layer 3 (dream, coordinator, ecosystem) modifies Python modules that depend on Layer 1 being complete. Layers 1 and 2 can be built in parallel.

**Tech Stack:** Python 3.12, sqlite3, dataclasses, json, pathlib, unittest with MagicMock. Tests run via `pytest tests/ -x -q`.

**Spec:** `docs/superpowers/specs/2026-04-01-agent-improvements-design.md`

---

## Layer 1: Infrastructure

### Task 1: AgentMessage dataclass and bus storage scaffolding

**Files:**
- Create: `claw_v2/bus.py`
- Create: `tests/test_bus.py`

- [ ] **Step 1: Write the failing test for AgentMessage creation and serialization**

```python
# tests/test_bus.py
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from claw_v2.bus import AgentBus, AgentMessage


class AgentMessageTests(unittest.TestCase):
    def test_create_message_with_defaults(self) -> None:
        msg = AgentMessage(
            id="test-123",
            from_agent="rook",
            to_agent="hex",
            intent="notify",
            topic="test_failure",
            payload={"file": "bot.py"},
            priority="normal",
        )
        self.assertEqual(msg.from_agent, "rook")
        self.assertEqual(msg.to_agent, "hex")
        self.assertEqual(msg.ttl_seconds, 3600)
        self.assertEqual(msg.correlation_id, "")
        self.assertIsNone(msg.consumed_at)

    def test_message_to_json_roundtrip(self) -> None:
        msg = AgentMessage(
            id="test-456",
            from_agent="alma",
            to_agent=None,
            intent="notify",
            topic="reminder_due",
            payload={"text": "call dentist"},
            priority="low",
            created_at=time.time(),
        )
        data = json.loads(json.dumps(msg.__dict__))
        restored = AgentMessage(**data)
        self.assertEqual(restored.id, msg.id)
        self.assertEqual(restored.payload, msg.payload)
        self.assertIsNone(restored.to_agent)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_bus.py -x -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claw_v2.bus'`

- [ ] **Step 3: Write AgentMessage dataclass and AgentBus scaffold**

```python
# claw_v2/bus.py
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_bus.py -x -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add claw_v2/bus.py tests/test_bus.py && git commit -m "feat(bus): add AgentMessage dataclass and AgentBus scaffold"
```

---

### Task 2: AgentBus.send() with eager fan-out for broadcasts

**Files:**
- Modify: `claw_v2/bus.py`
- Modify: `tests/test_bus.py`

- [ ] **Step 1: Write failing tests for send() — targeted and broadcast**

```python
# Append to tests/test_bus.py

class SendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.bus = AgentBus(bus_root=self.tmpdir)

    def test_send_targeted_writes_to_inbox(self) -> None:
        msg = _new_message(
            from_agent="rook",
            to_agent="hex",
            intent="notify",
            topic="test_failure",
            payload={"file": "bot.py"},
        )
        msg_id = self.bus.send(msg)
        inbox = self.tmpdir / "inbox" / "hex"
        files = list(inbox.glob("*.json"))
        self.assertEqual(len(files), 1)
        loaded = json.loads(files[0].read_text())
        self.assertEqual(loaded["topic"], "test_failure")

    def test_send_broadcast_fans_out_to_all_inboxes(self) -> None:
        msg = _new_message(
            from_agent="hex",
            to_agent=None,
            intent="notify",
            topic="pr_ready",
            payload={"pr": 42},
        )
        self.bus.send(msg)
        for agent in ("rook", "alma", "lux"):
            inbox = self.tmpdir / "inbox" / agent
            files = list(inbox.glob("*.json"))
            self.assertEqual(len(files), 1, f"Expected 1 message in {agent} inbox")
        # Sender does NOT get their own broadcast
        hex_inbox = self.tmpdir / "inbox" / "hex"
        self.assertEqual(len(list(hex_inbox.glob("*.json"))), 0)
        # Audit copy in broadcast/
        audit = self.tmpdir / "broadcast"
        self.assertEqual(len(list(audit.glob("*.json"))), 1)

    def test_send_returns_message_id(self) -> None:
        msg = _new_message(
            from_agent="alma",
            to_agent="rook",
            intent="request",
            topic="health_check",
            payload={},
        )
        msg_id = self.bus.send(msg)
        self.assertEqual(msg_id, msg.id)
```

Also add the import at the top of the test file:

```python
from claw_v2.bus import AgentBus, AgentMessage, _new_message
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_bus.py::SendTests -x -v`
Expected: FAIL — `AttributeError: 'AgentBus' object has no attribute 'send'`

- [ ] **Step 3: Implement send()**

Add to `AgentBus` in `claw_v2/bus.py`:

```python
    def send(self, message: AgentMessage) -> str:
        if message.to_agent is not None:
            # Targeted: write to recipient's inbox
            self._write_message(
                self.bus_root / "inbox" / message.to_agent / f"{message.id}.json",
                message,
            )
        else:
            # Broadcast: eager fan-out to every agent except sender
            for agent in KNOWN_AGENTS:
                if agent != message.from_agent:
                    self._write_message(
                        self.bus_root / "inbox" / agent / f"{message.id}.json",
                        message,
                    )
            # Audit copy
            self._write_message(
                self.bus_root / "broadcast" / f"{message.id}.json",
                message,
            )
        if message.intent == "escalate":
            logger.info("BUS escalation from %s: %s", message.from_agent, message.topic)
        return message.id
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_bus.py -x -v`
Expected: All passed

- [ ] **Step 5: Commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add claw_v2/bus.py tests/test_bus.py && git commit -m "feat(bus): implement send() with eager fan-out for broadcasts"
```

---

### Task 3: AgentBus.receive(), reply(), pending_count(), pending_urgent()

**Files:**
- Modify: `claw_v2/bus.py`
- Modify: `tests/test_bus.py`

- [ ] **Step 1: Write failing tests**

```python
# Append to tests/test_bus.py

class ReceiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.bus = AgentBus(bus_root=self.tmpdir)

    def test_receive_returns_messages_and_archives(self) -> None:
        msg = _new_message(from_agent="rook", to_agent="hex", intent="notify", topic="alert", payload={})
        self.bus.send(msg)
        received = self.bus.receive("hex")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].topic, "alert")
        # Message moved to archive
        self.assertEqual(len(list((self.tmpdir / "inbox" / "hex").glob("*.json"))), 0)
        self.assertEqual(len(list((self.tmpdir / "archive").glob("*.json"))), 1)

    def test_receive_skips_expired_messages(self) -> None:
        msg = _new_message(from_agent="rook", to_agent="hex", intent="notify", topic="old", payload={}, ttl_seconds=1)
        msg.created_at = time.time() - 10  # expired 9 seconds ago
        self.bus.send(msg)
        received = self.bus.receive("hex")
        self.assertEqual(len(received), 0)

    def test_receive_newest_first(self) -> None:
        for i in range(3):
            msg = _new_message(from_agent="rook", to_agent="hex", intent="notify", topic=f"t{i}", payload={})
            msg.created_at = time.time() + i
            self.bus.send(msg)
        received = self.bus.receive("hex")
        topics = [m.topic for m in received]
        self.assertEqual(topics, ["t2", "t1", "t0"])


class ReplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.bus = AgentBus(bus_root=self.tmpdir)

    def test_reply_links_via_correlation_id(self) -> None:
        original = _new_message(from_agent="rook", to_agent="hex", intent="request", topic="help", payload={})
        self.bus.send(original)
        received = self.bus.receive("hex")
        reply_id = self.bus.reply(received[0], content={"answer": "done"}, from_agent="hex")
        replies = self.bus.receive("rook")
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].intent, "reply")
        self.assertEqual(replies[0].correlation_id, original.correlation_id)
        self.assertEqual(replies[0].payload, {"answer": "done"})


class PendingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.bus = AgentBus(bus_root=self.tmpdir)

    def test_pending_count(self) -> None:
        for i in range(3):
            msg = _new_message(from_agent="rook", to_agent="hex", intent="notify", topic=f"t{i}", payload={})
            self.bus.send(msg)
        self.assertEqual(self.bus.pending_count("hex"), 3)
        self.assertEqual(self.bus.pending_count("alma"), 0)

    def test_pending_urgent(self) -> None:
        normal = _new_message(from_agent="rook", to_agent="hex", intent="notify", topic="low", payload={}, priority="normal")
        urgent = _new_message(from_agent="rook", to_agent="alma", intent="escalate", topic="fire", payload={}, priority="urgent")
        self.bus.send(normal)
        self.bus.send(urgent)
        urgents = self.bus.pending_urgent()
        self.assertEqual(len(urgents), 1)
        self.assertEqual(urgents[0].topic, "fire")
```

- [ ] **Step 2: Run to verify fails**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_bus.py::ReceiveTests -x -v`
Expected: FAIL

- [ ] **Step 3: Implement receive(), reply(), pending_count(), pending_urgent()**

Add to `AgentBus` in `claw_v2/bus.py`:

```python
    def receive(self, agent_name: str, *, max_messages: int = 20) -> list[AgentMessage]:
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
        inbox = self.bus_root / "inbox" / agent_name
        return len(list(inbox.glob("*.json")))

    def pending_urgent(self) -> list[AgentMessage]:
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
```

- [ ] **Step 4: Run full bus test suite**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_bus.py -x -v`
Expected: All passed

- [ ] **Step 5: Commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add claw_v2/bus.py tests/test_bus.py && git commit -m "feat(bus): implement receive, reply, pending_count, pending_urgent"
```

---

### Task 4: AgentBus.scan_expired_requests() and cleanup()

**Files:**
- Modify: `claw_v2/bus.py`
- Modify: `tests/test_bus.py`

- [ ] **Step 1: Write failing tests**

```python
# Append to tests/test_bus.py

class ScanExpiredRequestsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.bus = AgentBus(bus_root=self.tmpdir)

    def test_detects_expired_request_without_reply(self) -> None:
        msg = _new_message(from_agent="rook", to_agent="hex", intent="request", topic="help", payload={}, ttl_seconds=1)
        msg.created_at = time.time() - 10
        self.bus.send(msg)
        expired = self.bus.scan_expired_requests()
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0].topic, "help")

    def test_ignores_notify_messages(self) -> None:
        msg = _new_message(from_agent="rook", to_agent="hex", intent="notify", topic="info", payload={}, ttl_seconds=1)
        msg.created_at = time.time() - 10
        self.bus.send(msg)
        expired = self.bus.scan_expired_requests()
        self.assertEqual(len(expired), 0)

    def test_ignores_non_expired_requests(self) -> None:
        msg = _new_message(from_agent="rook", to_agent="hex", intent="request", topic="help", payload={}, ttl_seconds=9999)
        self.bus.send(msg)
        expired = self.bus.scan_expired_requests()
        self.assertEqual(len(expired), 0)


class CleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.bus = AgentBus(bus_root=self.tmpdir)

    def test_removes_old_archived_messages(self) -> None:
        msg = _new_message(from_agent="rook", to_agent="hex", intent="notify", topic="old", payload={})
        self.bus.send(msg)
        self.bus.receive("hex")  # archives it
        # Backdate the archive file
        archive_files = list((self.tmpdir / "archive").glob("*.json"))
        self.assertEqual(len(archive_files), 1)
        import os
        old_time = time.time() - (8 * 86400)  # 8 days ago
        os.utime(archive_files[0], (old_time, old_time))
        removed = self.bus.cleanup(max_age_days=7)
        self.assertEqual(removed, 1)
        self.assertEqual(len(list((self.tmpdir / "archive").glob("*.json"))), 0)
```

- [ ] **Step 2: Run to verify fails**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_bus.py::ScanExpiredRequestsTests -x -v`
Expected: FAIL

- [ ] **Step 3: Implement scan_expired_requests() and cleanup()**

Add to `AgentBus` in `claw_v2/bus.py`:

```python
    def scan_expired_requests(self) -> list[AgentMessage]:
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
        archive = self.bus_root / "archive"
        cutoff = time.time() - (max_age_days * 86400)
        removed = 0
        for path in archive.glob("*.json"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        return removed
```

- [ ] **Step 4: Run full bus test suite**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_bus.py -x -v`
Expected: All passed

- [ ] **Step 5: Commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add claw_v2/bus.py tests/test_bus.py && git commit -m "feat(bus): add scan_expired_requests and cleanup"
```

---

### Task 5: Kairos executable actions — dispatch table and handlers

**Files:**
- Modify: `claw_v2/kairos.py`
- Modify: `tests/test_kairos.py`

- [ ] **Step 1: Write failing tests for action execution**

```python
# Append to tests/test_kairos.py
import json
import tempfile
from pathlib import Path
from claw_v2.bus import AgentBus


class ExecuteActionTests(unittest.TestCase):
    def test_dispatch_to_agent_sends_bus_message(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        bus = AgentBus(bus_root=tmpdir)
        svc, router, heartbeat, observe = _make_service(bus=bus, approvals=MagicMock())
        decision = TickDecision(
            action="dispatch_to_agent",
            reason="test failure detected",
            detail=json.dumps({"to_agent": "hex", "topic": "test_failure", "payload": {"file": "bot.py"}}),
        )
        result = svc._execute(decision, budget=10.0)
        self.assertEqual(result.action, "dispatch_to_agent")
        self.assertEqual(result.error, "")
        self.assertEqual(bus.pending_count("hex"), 1)

    def test_pause_agent_pauses_and_notifies(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        bus = AgentBus(bus_root=tmpdir)
        mock_approvals = MagicMock()
        svc, router, heartbeat, observe = _make_service(bus=bus, approvals=mock_approvals)
        decision = TickDecision(
            action="pause_agent",
            reason="budget exceeded",
            detail=json.dumps({"agent_name": "lux", "reason": "cost limit"}),
        )
        result = svc._execute(decision, budget=10.0)
        self.assertEqual(result.action, "pause_agent")
        # Should emit agent_paused event
        observe.emit.assert_any_call("agent_paused", payload={"agent_name": "lux", "reason": "cost limit"})

    def test_unknown_action_logs_and_returns(self) -> None:
        svc, _, _, observe = _make_service(bus=MagicMock(), approvals=MagicMock())
        decision = TickDecision(action="unknown_thing", reason="test")
        result = svc._execute(decision, budget=10.0)
        self.assertEqual(result.action, "unknown_thing")
        self.assertIn("unknown", result.error)
```

- [ ] **Step 2: Run to verify fails**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_kairos.py::ExecuteActionTests -x -v`
Expected: FAIL — `_make_service()` doesn't accept `bus` or `approvals`

- [ ] **Step 3: Update _make_service in test and update KairosService**

First update the test helper in `tests/test_kairos.py`:

```python
def _make_service(**overrides):
    router = MagicMock()
    heartbeat = MagicMock()
    observe = MagicMock()
    heartbeat.collect.return_value = MagicMock(
        pending_approvals=0,
        pending_approval_ids=[],
        agents={},
        lane_metrics={},
    )
    defaults = dict(
        router=router,
        heartbeat=heartbeat,
        observe=observe,
        action_budget=15.0,
        brief=True,
        bus=overrides.pop("bus", None),
        approvals=overrides.pop("approvals", None),
    )
    defaults.update(overrides)
    svc = KairosService(**defaults)
    return svc, router, heartbeat, observe
```

Then update `claw_v2/kairos.py` — add `bus` and `approvals` to `__init__`, replace `_execute` with dispatch table:

```python
# Add imports at top of kairos.py
from claw_v2.bus import AgentBus, _new_message

# Update __init__ signature
def __init__(
    self,
    *,
    router: Any,
    heartbeat: Any,
    observe: Any,
    bus: Any | None = None,
    approvals: Any | None = None,
    action_budget: float = DEFAULT_ACTION_BUDGET,
    brief: bool = True,
) -> None:
    self.router = router
    self.heartbeat = heartbeat
    self.observe = observe
    self.bus = bus
    self.approvals = approvals
    self.action_budget = action_budget
    self.brief = brief
    self.state = KairosState()

# Replace _execute method
def _execute(self, decision: TickDecision, *, budget: float) -> TickDecision:
    start = time.time()
    handlers = {
        "notify_user": self._handle_notify_user,
        "dispatch_to_agent": self._handle_dispatch_to_agent,
        "approve_pending": self._handle_approve_pending,
        "run_skill": self._handle_run_skill,
        "pause_agent": self._handle_pause_agent,
        "escalate_to_human": self._handle_escalate_to_human,
    }
    handler = handlers.get(decision.action)
    if handler is None:
        decision.error = f"unknown action: {decision.action}"
        logger.warning("KAIROS unknown action: %s", decision.action)
    else:
        try:
            handler(decision)
        except Exception as exc:
            decision.error = str(exc)
            logger.exception("KAIROS action %s failed", decision.action)
    decision.duration_seconds = time.time() - start
    return decision

def _handle_notify_user(self, decision: TickDecision) -> None:
    logger.info("KAIROS notify_user: %s", decision.detail)
    self.observe.emit("kairos_notify_user", payload={"message": decision.detail})

def _handle_dispatch_to_agent(self, decision: TickDecision) -> None:
    if self.bus is None:
        raise RuntimeError("Bus not configured")
    data = json.loads(decision.detail)
    msg = _new_message(
        from_agent="kairos",
        to_agent=data["to_agent"],
        intent="notify",
        topic=data["topic"],
        payload=data.get("payload", {}),
        priority="normal",
    )
    self.bus.send(msg)

def _handle_approve_pending(self, decision: TickDecision) -> None:
    if self.approvals is None:
        raise RuntimeError("Approvals not configured")
    data = json.loads(decision.detail)
    approval_id = data["approval_id"]
    self.approvals.approve(approval_id)
    self.observe.emit("kairos_auto_approved", payload={"approval_id": approval_id})

def _handle_run_skill(self, decision: TickDecision) -> None:
    data = json.loads(decision.detail)
    logger.info("KAIROS run_skill: agent=%s skill=%s", data["agent"], data["skill"])
    self.observe.emit("kairos_run_skill", payload=data)

def _handle_pause_agent(self, decision: TickDecision) -> None:
    data = json.loads(decision.detail)
    agent_name = data["agent_name"]
    reason = data["reason"]
    self.observe.emit("agent_paused", payload={"agent_name": agent_name, "reason": reason})
    logger.info("KAIROS paused agent %s: %s", agent_name, reason)

def _handle_escalate_to_human(self, decision: TickDecision) -> None:
    logger.info("KAIROS escalate: %s", decision.detail)
    self.observe.emit("kairos_escalate", payload={"message": decision.detail})
```

Also add `import json` at the top of `kairos.py` (already imported).

- [ ] **Step 4: Run full kairos test suite**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_kairos.py -x -v`
Expected: All passed (existing + new tests)

- [ ] **Step 5: Commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add claw_v2/kairos.py tests/test_kairos.py && git commit -m "feat(kairos): add executable action dispatch table with 6 handlers"
```

---

### Task 6: Enhanced Kairos _gather_context() with bus and cost data

**Files:**
- Modify: `claw_v2/kairos.py`
- Modify: `claw_v2/observe.py`
- Modify: `tests/test_kairos.py`

- [ ] **Step 1: Write failing test for enhanced context**

```python
# Append to tests/test_kairos.py

class EnhancedContextTests(unittest.TestCase):
    def test_context_includes_urgent_bus_messages(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        bus = AgentBus(bus_root=tmpdir)
        from claw_v2.bus import _new_message
        msg = _new_message(from_agent="rook", to_agent="hex", intent="escalate", topic="fire", payload={}, priority="urgent")
        bus.send(msg)
        svc, _, _, _ = _make_service(bus=bus)
        ctx = svc._gather_context()
        self.assertIn("Urgent bus messages: 1", ctx)
        self.assertIn("fire", ctx)

    def test_context_includes_expired_requests(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        bus = AgentBus(bus_root=tmpdir)
        from claw_v2.bus import _new_message
        msg = _new_message(from_agent="rook", to_agent="hex", intent="request", topic="help", payload={}, ttl_seconds=1)
        msg.created_at = time.time() - 10
        bus.send(msg)
        svc, _, _, _ = _make_service(bus=bus)
        ctx = svc._gather_context()
        self.assertIn("Expired requests: 1", ctx)
```

- [ ] **Step 2: Run to verify fails**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_kairos.py::EnhancedContextTests -x -v`
Expected: FAIL

- [ ] **Step 3: Add cost_per_agent_today to ObserveStream and update _gather_context**

Add to `claw_v2/observe.py`:

```python
    def cost_per_agent_today(self) -> dict[str, float]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT json_extract(payload, '$.agent_name') as agent,
                       COALESCE(SUM(json_extract(payload, '$.cost_estimate')), 0.0) as cost
                FROM observe_stream
                WHERE event_type = 'llm_decision'
                  AND timestamp >= date('now', 'start of day')
                GROUP BY agent
                """,
            ).fetchall()
        return {row[0]: row[1] for row in rows if row[0]}
```

Update `_gather_context` in `claw_v2/kairos.py`:

```python
def _gather_context(self) -> str:
    parts: list[str] = []

    # Heartbeat snapshot
    try:
        snapshot = self.heartbeat.collect()
        parts.append(f"Pending approvals: {snapshot.pending_approvals}")
        if snapshot.pending_approval_ids:
            parts.append(f"Approval IDs: {', '.join(snapshot.pending_approval_ids[:5])}")
        agent_lines = []
        for name, info in snapshot.agents.items():
            status = "paused" if info.get("paused") else "active"
            metric = info.get("last_metric", "?")
            agent_lines.append(f"  {name}: {status}, metric={metric}")
        if agent_lines:
            parts.append("Agents:\n" + "\n".join(agent_lines))
    except Exception:
        parts.append("Heartbeat: unavailable")

    # Recent events
    try:
        events = self.observe.recent_events(limit=10)
        if events:
            event_lines = [
                f"  [{e.get('event_type', '?')}] {str(e.get('payload', ''))[:100]}"
                for e in events[:5]
            ]
            parts.append("Recent events:\n" + "\n".join(event_lines))
    except Exception:
        parts.append("Recent events: unavailable")

    # Bus: urgent messages
    if self.bus is not None:
        try:
            urgent = self.bus.pending_urgent()
            if urgent:
                parts.append(f"Urgent bus messages: {len(urgent)}")
                for msg in urgent[:3]:
                    parts.append(f"  [{msg.from_agent}->{msg.to_agent}] {msg.topic}: {str(msg.payload)[:100]}")
            expired = self.bus.scan_expired_requests()
            if expired:
                parts.append(f"Expired requests: {len(expired)}")
                for msg in expired[:3]:
                    parts.append(f"  [{msg.from_agent}->{msg.to_agent}] {msg.topic}")
        except Exception:
            parts.append("Bus: unavailable")

    # Cost per agent
    try:
        costs = self.observe.cost_per_agent_today()
        if costs:
            cost_lines = [f"  {name}: ${cost:.2f}" for name, cost in costs.items()]
            parts.append("Cost today:\n" + "\n".join(cost_lines))
    except Exception:
        pass

    parts.append(f"Ticks so far: {self.state.ticks}, actions taken: {self.state.actions_taken}")
    return "\n".join(parts)
```

- [ ] **Step 4: Run full test suites**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_kairos.py tests/test_bus.py -x -v`
Expected: All passed

- [ ] **Step 5: Commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add claw_v2/kairos.py claw_v2/observe.py tests/test_kairos.py && git commit -m "feat(kairos): enhance context with bus urgents, expired requests, agent costs"
```

---

### Task 7: Live Agent Registry in HeartbeatService.emit()

**Files:**
- Modify: `claw_v2/heartbeat.py`
- Modify: `tests/test_bus.py` (or create `tests/test_heartbeat.py` — use existing test patterns)

- [ ] **Step 1: Write failing test**

```python
# Append to a new file tests/test_heartbeat_registry.py
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.heartbeat import HeartbeatService, _compute_health, update_agent_registry


class ComputeHealthTests(unittest.TestCase):
    def test_ok_when_active_no_errors(self) -> None:
        info = {"paused": False, "cost_today": 1.0, "daily_budget": 10.0}
        self.assertEqual(_compute_health(info), "OK")

    def test_warn_budget(self) -> None:
        info = {"paused": False, "cost_today": 9.0, "daily_budget": 10.0}
        self.assertEqual(_compute_health(info), "WARN:budget")

    def test_critical_when_paused(self) -> None:
        info = {"paused": True}
        self.assertEqual(_compute_health(info), "CRITICAL")


class RegistryWriteTests(unittest.TestCase):
    def test_emit_writes_agents_md(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        registry_path = tmpdir / "AGENTS.md"
        metrics = MagicMock()
        metrics.snapshot.return_value = {}
        approvals = MagicMock()
        approvals.list_pending.return_value = []
        agent_store = MagicMock()
        agent_store.list_agents.return_value = ["hex"]
        agent_store.load_state.return_value = {
            "agent_class": "operator",
            "paused": False,
            "last_verified_state": {"metric": 0.95},
        }
        observe = MagicMock()
        svc = HeartbeatService(
            metrics=metrics,
            approvals=approvals,
            agent_store=agent_store,
            observe=observe,
            registry_path=registry_path,
        )
        svc.emit()
        self.assertTrue(registry_path.exists())
        content = registry_path.read_text()
        self.assertIn("hex", content)
        self.assertIn("Agent", content)
```

- [ ] **Step 2: Run to verify fails**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_heartbeat_registry.py -x -v`
Expected: FAIL — `_compute_health` and `update_agent_registry` don't exist, `registry_path` not accepted

- [ ] **Step 3: Add _compute_health, update_agent_registry, and update HeartbeatService**

Add to `claw_v2/heartbeat.py`:

```python
from pathlib import Path

def _compute_health(info: dict) -> str:
    if info.get("paused"):
        return "CRITICAL"
    cost = info.get("cost_today", 0)
    budget = info.get("daily_budget", 10.0)
    if budget > 0 and cost / budget > 0.8:
        return "WARN:budget"
    if info.get("has_errors"):
        return "WARN:errors"
    return "OK"


def update_agent_registry(snapshot: HeartbeatSnapshot, registry_path: Path) -> None:
    header = "| Agent | Model | Status | Last Action | Last Metric | Cost Today | Health |\n"
    separator = "|-------|-------|--------|-------------|-------------|------------|--------|\n"
    rows = []
    for name, info in sorted(snapshot.agents.items()):
        status = "paused" if info.get("paused") else "active"
        last_action = info.get("last_action", "-")
        last_metric = info.get("last_metric", "-")
        cost = f"${info.get('cost_today', 0):.2f}"
        health = _compute_health(info)
        model = info.get("model", "-")
        rows.append(f"| {name} | {model} | {status} | {last_action} | {last_metric} | {cost} | {health} |")
    content = f"# Agent Registry\n\nAuto-updated every heartbeat.\n\n{header}{separator}" + "\n".join(rows) + "\n"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(content, encoding="utf-8")
```

Update `HeartbeatService.__init__` to accept `registry_path`:

```python
def __init__(
    self,
    *,
    metrics: MetricsTracker,
    approvals: ApprovalManager,
    agent_store: FileAgentStore,
    observe: ObserveStream | None = None,
    registry_path: Path | None = None,
) -> None:
    self.metrics = metrics
    self.approvals = approvals
    self.agent_store = agent_store
    self.observe = observe
    self.registry_path = registry_path
```

Update `emit()`:

```python
def emit(self) -> HeartbeatSnapshot:
    snapshot = self.collect()
    if self.observe is not None:
        self.observe.emit("heartbeat", payload=asdict(snapshot))
    if self.registry_path is not None:
        update_agent_registry(snapshot, self.registry_path)
        if self.observe is not None:
            self.observe.emit("agent_registry_updated")
    return snapshot
```

- [ ] **Step 4: Run all heartbeat and existing tests**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_heartbeat_registry.py tests/test_kairos.py -x -v`
Expected: All passed

- [ ] **Step 5: Commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add claw_v2/heartbeat.py tests/test_heartbeat_registry.py && git commit -m "feat(heartbeat): add live agent registry write in emit()"
```

---

### Task 8: Wire bus and registry into main.py runtime

**Files:**
- Modify: `claw_v2/main.py`

- [ ] **Step 1: Update ClawRuntime dataclass and build_runtime()**

Add to `ClawRuntime`:

```python
from claw_v2.bus import AgentBus

@dataclass(slots=True)
class ClawRuntime:
    config: AppConfig
    memory: MemoryStore
    observe: ObserveStream
    metrics: MetricsTracker
    approvals: ApprovalManager
    agent_store: FileAgentStore
    router: LLMRouter
    brain: BrainService
    auto_research: AutoResearchAgentService
    sub_agents: SubAgentService
    coordinator: CoordinatorService
    kairos: KairosService
    buddy: BuddyService
    heartbeat: HeartbeatService
    scheduler: CronScheduler
    daemon: ClawDaemon
    bot: BotService
    bus: AgentBus  # NEW
```

Update `build_runtime()` — add after `agent_store` creation:

```python
    bus = AgentBus(bus_root=config.agent_state_root / "_bus")
```

Update heartbeat creation:

```python
    heartbeat = HeartbeatService(
        metrics=metrics,
        approvals=approvals,
        agent_store=agent_store,
        observe=observe,
        registry_path=Path(config.workspace_root) / "claw_v2" / "AGENTS.md",
    )
```

Update kairos creation:

```python
    kairos = KairosService(router=router, heartbeat=heartbeat, observe=observe, bus=bus, approvals=approvals)
```

Add `bus=bus` to the ClawRuntime return.

- [ ] **Step 2: Run full test suite to verify nothing broke**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/ -x -q --tb=short`
Expected: All existing tests pass

- [ ] **Step 3: Commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add claw_v2/main.py && git commit -m "feat(main): wire AgentBus into runtime, connect to kairos and heartbeat"
```

---

## Layer 2: Skills (9 SKILL.md files)

Skills are content files — no Python changes. Each task creates one SKILL.md using the same frontmatter format as `agents/hex/skills/bug-triage/SKILL.md`.

### Task 9: Hex — code-review skill

**Files:**
- Create: `agents/hex/skills/code-review/SKILL.md`

- [ ] **Step 1: Create the skill file**

Write the complete SKILL.md following the spec at `docs/superpowers/specs/2026-04-01-agent-improvements-design.md` lines 265-307, using the same YAML frontmatter format as `agents/hex/skills/bug-triage/SKILL.md`.

The file content must include: frontmatter with name/description, purpose, trigger, inputs table, process steps 1-6 (security scan, logic, performance, style, test coverage), output format with findings table, and done criteria checklist. All content from spec section 2.1 "code-review".

- [ ] **Step 2: Verify file exists and has frontmatter**

Run: `head -5 /Users/hector/Projects/Dr.-strange/agents/hex/skills/code-review/SKILL.md`
Expected: `---` followed by `name: code-review`

- [ ] **Step 3: Commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add agents/hex/skills/code-review/SKILL.md && git commit -m "feat(hex): add code-review skill"
```

---

### Task 10: Hex — dependency-audit skill

**Files:**
- Create: `agents/hex/skills/dependency-audit/SKILL.md`

- [ ] **Step 1: Create the skill file**

Content from spec lines 311-353. Frontmatter, inputs table, 6-step process (parse deps, CVE check, freshness, license, unused detection, pin analysis), output format, done criteria.

- [ ] **Step 2: Verify**

Run: `head -5 /Users/hector/Projects/Dr.-strange/agents/hex/skills/dependency-audit/SKILL.md`

- [ ] **Step 3: Commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add agents/hex/skills/dependency-audit/SKILL.md && git commit -m "feat(hex): add dependency-audit skill"
```

---

### Task 11: Hex — refactor-plan skill

**Files:**
- Create: `agents/hex/skills/refactor-plan/SKILL.md`

- [ ] **Step 1: Create the skill file**

Content from spec lines 357-404. Frontmatter, inputs, 5-step process (smell detection, cluster, dependency analysis, order, atomicity check), output format with steps, done criteria.

- [ ] **Step 2: Verify and commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add agents/hex/skills/refactor-plan/SKILL.md && git commit -m "feat(hex): add refactor-plan skill"
```

---

### Task 12: Rook — incident-response skill

**Files:**
- Create: `agents/rook/skills/incident-response/SKILL.md`

- [ ] **Step 1: Create the skill file**

Content from spec lines 410-468. Frontmatter, inputs, 6-step process (evidence collection, timeline, severity SEV1-3, root cause, Tier 1 mitigations, runbook), output format, done criteria.

- [ ] **Step 2: Verify and commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add agents/rook/skills/incident-response/SKILL.md && git commit -m "feat(rook): add incident-response skill"
```

---

### Task 13: Rook — log-analysis skill

**Files:**
- Create: `agents/rook/skills/log-analysis/SKILL.md`

- [ ] **Step 1: Create the skill file**

Content from spec lines 473-521. Frontmatter, inputs, 5-step process (ingest, pattern clustering, anomaly detection, correlation, rank), output format, done criteria.

- [ ] **Step 2: Verify and commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add agents/rook/skills/log-analysis/SKILL.md && git commit -m "feat(rook): add log-analysis skill"
```

---

### Task 14: Rook — cron-doctor skill

**Files:**
- Create: `agents/rook/skills/cron-doctor/SKILL.md`

- [ ] **Step 1: Create the skill file**

Content from spec lines 525-576. Frontmatter, inputs, 5-step process (history analysis, pattern detection, dependency check, conflict check, fix proposal), output format, done criteria.

- [ ] **Step 2: Verify and commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add agents/rook/skills/cron-doctor/SKILL.md && git commit -m "feat(rook): add cron-doctor skill"
```

---

### Task 15: Alma — pending-items skill

**Files:**
- Create: `agents/alma/skills/pending-items/SKILL.md`

- [ ] **Step 1: Create the skill file**

Content from spec lines 582-621. Frontmatter, inputs, 4-step process (scan, dedup, enrich, prioritize), output format in Spanish, done criteria.

- [ ] **Step 2: Verify and commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add agents/alma/skills/pending-items/SKILL.md && git commit -m "feat(alma): add pending-items skill"
```

---

### Task 16: Alma — weekly-retro skill

**Files:**
- Create: `agents/alma/skills/weekly-retro/SKILL.md`

- [ ] **Step 1: Create the skill file**

Content from spec lines 625-667. Frontmatter, inputs, 5-step process (achievements, carryover, pattern detection, insight, suggestion), output format in Spanish, done criteria.

- [ ] **Step 2: Verify and commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add agents/alma/skills/weekly-retro/SKILL.md && git commit -m "feat(alma): add weekly-retro skill"
```

---

### Task 17: Alma — context-bridge skill

**Files:**
- Create: `agents/alma/skills/context-bridge/SKILL.md`

- [ ] **Step 1: Create the skill file**

Content from spec lines 671-701. Frontmatter, inputs, 5-step process (intent detection, context assembly, translation per agent, privacy filter, delivery), output as AgentMessage, done criteria.

- [ ] **Step 2: Verify and commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add agents/alma/skills/context-bridge/SKILL.md && git commit -m "feat(alma): add context-bridge skill"
```

---

## Layer 3: Consolidation

### Task 18: Agent-scoped facts — memory.py schema migration

**Files:**
- Modify: `claw_v2/memory.py`
- Modify: `tests/test_bus.py` or create `tests/test_memory_scoped.py`

- [ ] **Step 1: Write failing test for agent-scoped facts**

```python
# tests/test_memory_scoped.py
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.memory import MemoryStore


class AgentScopedFactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = Path(tempfile.mkdtemp()) / "test.db"
        self.store = MemoryStore(self.db_path)

    def test_store_fact_with_agent_name(self) -> None:
        self.store.store_fact("bug.recurring", "null ref in bot.py", source="hex", agent_name="hex")
        facts = self.store.search_facts("bug", agent_name="hex")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["agent_name"], "hex")

    def test_search_facts_filters_by_agent(self) -> None:
        self.store.store_fact("cron.conflict", "SEO vs health", source="rook", agent_name="rook")
        self.store.store_fact("bug.null", "bot.py line 42", source="hex", agent_name="hex")
        rook_facts = self.store.search_facts("", agent_name="rook")
        self.assertEqual(len(rook_facts), 1)
        self.assertEqual(rook_facts[0]["key"], "cron.conflict")

    def test_search_without_agent_returns_all(self) -> None:
        self.store.store_fact("a", "1", source="s", agent_name="hex")
        self.store.store_fact("b", "2", source="s", agent_name="rook")
        all_facts = self.store.search_facts("")
        self.assertEqual(len(all_facts), 2)

    def test_default_agent_name_is_system(self) -> None:
        self.store.store_fact("global.fact", "shared", source="dream")
        facts = self.store.search_facts("global")
        self.assertEqual(facts[0]["agent_name"], "system")
```

- [ ] **Step 2: Run to verify fails**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_memory_scoped.py -x -v`
Expected: FAIL — `store_fact()` doesn't accept `agent_name`

- [ ] **Step 3: Add agent_name column and update MemoryStore methods**

Update `SCHEMA` in `claw_v2/memory.py` — add migration after schema creation:

```python
SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL,
    source_trust TEXT NOT NULL DEFAULT 'untrusted',
    confidence REAL NOT NULL DEFAULT 0.5,
    entity_tags TEXT NOT NULL DEFAULT '[]',
    valid_from TEXT,
    valid_until TEXT,
    conflict_flag INTEGER NOT NULL DEFAULT 0,
    agent_name TEXT NOT NULL DEFAULT 'system',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS provider_sessions (
    app_session_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_session_id TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (app_session_id, provider)
);

CREATE TABLE IF NOT EXISTS fact_embeddings (
    fact_id INTEGER PRIMARY KEY REFERENCES facts(id),
    embedding TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cron_state (
    job_name TEXT PRIMARY KEY,
    last_run_at REAL NOT NULL DEFAULT 0.0,
    runs INTEGER NOT NULL DEFAULT 0
);
"""

_MIGRATION_ADD_AGENT_NAME = """
ALTER TABLE facts ADD COLUMN agent_name TEXT NOT NULL DEFAULT 'system';
"""
```

Add migration logic in `MemoryStore.__init__`:

```python
def __init__(self, db_path: Path | str) -> None:
    self.db_path = Path(db_path)
    self.db_path.parent.mkdir(parents=True, exist_ok=True)
    self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
    self._conn.row_factory = sqlite3.Row
    self._conn.executescript(SCHEMA)
    self._migrate()
    self._lock = threading.Lock()

def _migrate(self) -> None:
    cursor = self._conn.execute("PRAGMA table_info(facts)")
    columns = {row[1] for row in cursor.fetchall()}
    if "agent_name" not in columns:
        try:
            self._conn.execute(_MIGRATION_ADD_AGENT_NAME)
            self._conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
```

Update `store_fact`:

```python
def store_fact(
    self,
    key: str,
    value: str,
    *,
    source: str,
    source_trust: str = "untrusted",
    confidence: float = 0.5,
    entity_tags: Iterable[str] = (),
    valid_from: str | None = None,
    valid_until: str | None = None,
    agent_name: str = "system",
) -> None:
    with self._lock:
        self._conn.execute(
            """
            INSERT INTO facts (
                key, value, source, source_trust, confidence, entity_tags, valid_from, valid_until, agent_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                value,
                source,
                source_trust,
                confidence,
                json.dumps(list(entity_tags)),
                valid_from,
                valid_until,
                agent_name,
            ),
        )
        self._conn.commit()
```

Update `search_facts`:

```python
def search_facts(self, query: str, limit: int = 10, agent_name: str | None = None) -> list[dict]:
    if agent_name:
        rows = self._conn.execute(
            """
            SELECT key, value, source, source_trust, confidence, entity_tags, agent_name
            FROM facts
            WHERE (key LIKE ? OR value LIKE ?) AND agent_name = ?
            ORDER BY confidence DESC, id DESC
            LIMIT ?
            """,
            (f"%{query}%", f"%{query}%", agent_name, limit),
        ).fetchall()
    else:
        rows = self._conn.execute(
            """
            SELECT key, value, source, source_trust, confidence, entity_tags, agent_name
            FROM facts
            WHERE key LIKE ? OR value LIKE ?
            ORDER BY confidence DESC, id DESC
            LIMIT ?
            """,
            (f"%{query}%", f"%{query}%", limit),
        ).fetchall()
    return [dict(row) for row in rows]
```

- [ ] **Step 4: Run memory tests and full suite**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_memory_scoped.py tests/test_dream.py -x -v`
Expected: All passed

- [ ] **Step 5: Commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add claw_v2/memory.py tests/test_memory_scoped.py && git commit -m "feat(memory): add agent_name column to facts table with migration"
```

---

### Task 19: Cross-agent dream — export and import

**Files:**
- Modify: `claw_v2/dream.py`
- Modify: `tests/test_dream.py`

- [ ] **Step 1: Write failing tests for export/import**

```python
# Append to tests/test_dream.py
import json
import tempfile
from pathlib import Path


class ExportSharedTests(unittest.TestCase):
    def test_exports_high_confidence_facts(self) -> None:
        svc, memory, observe, _ = _make_service()
        svc.agent_name = "rook"
        svc.shared_memory_root = Path(tempfile.mkdtemp())
        facts = [
            {"key": "cron.conflict", "value": "SEO vs health", "confidence": 0.8, "source": "rook"},
            {"key": "low.fact", "value": "meh", "confidence": 0.3, "source": "rook"},
        ]
        count = svc._export_shared(facts)
        self.assertEqual(count, 1)  # only high confidence
        export_file = svc.shared_memory_root / "rook_exports.jsonl"
        self.assertTrue(export_file.exists())
        lines = export_file.read_text().strip().split("\n")
        self.assertEqual(len(lines), 1)
        data = json.loads(lines[0])
        self.assertEqual(data["key"], "cron.conflict")


class ImportSharedTests(unittest.TestCase):
    def test_imports_matching_tags(self) -> None:
        shared_root = Path(tempfile.mkdtemp())
        # Write an export from rook
        export = {"key": "cron.issue", "value": "conflict", "source_agent": "rook", "confidence": 0.8, "timestamp": 1000, "tags": ["infra", "cron"]}
        (shared_root / "rook_exports.jsonl").write_text(json.dumps(export) + "\n")
        svc, memory, _, _ = _make_service()
        svc.agent_name = "hex"
        svc.shared_memory_root = shared_root
        svc.import_tags = ["infra", "cron", "code"]
        memory.search_facts.return_value = []  # no existing facts
        imported = svc._import_shared()
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0]["key"], "cron.issue")

    def test_skips_personal_tags_for_non_alma(self) -> None:
        shared_root = Path(tempfile.mkdtemp())
        export = {"key": "personal.pref", "value": "morning meetings", "source_agent": "alma", "confidence": 0.9, "timestamp": 1000, "tags": ["personal"]}
        (shared_root / "alma_exports.jsonl").write_text(json.dumps(export) + "\n")
        svc, memory, _, _ = _make_service()
        svc.agent_name = "hex"
        svc.shared_memory_root = shared_root
        svc.import_tags = ["personal", "code"]  # even if listed
        memory.search_facts.return_value = []
        imported = svc._import_shared()
        self.assertEqual(len(imported), 0)  # personal blocked for hex
```

- [ ] **Step 2: Run to verify fails**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_dream.py::ExportSharedTests -x -v`
Expected: FAIL — `agent_name` not an attribute

- [ ] **Step 3: Update AutoDreamService with agent_name, shared_memory_root, import/export**

Add new parameters to `AutoDreamService.__init__`:

```python
def __init__(
    self,
    *,
    memory: Any,
    observe: Any,
    router: Any,
    agent_name: str = "system",
    shared_memory_root: Path | None = None,
    import_tags: list[str] | None = None,
    min_hours_between_dreams: float = 24.0,
    min_sessions_between_dreams: int = 5,
    max_facts: int = 200,
    lane: str = "research",
) -> None:
    self.memory = memory
    self.observe = observe
    self.router = router
    self.agent_name = agent_name
    self.shared_memory_root = shared_memory_root or Path.home() / ".claw" / "shared-memory"
    self.import_tags = import_tags or []
    self.min_hours_between_dreams = min_hours_between_dreams
    self.min_sessions_between_dreams = min_sessions_between_dreams
    self.max_facts = max_facts
    self.lane = lane
    self._last_dream_at: float = 0.0
    self._sessions_since_dream: int = 0
```

Add the two new methods:

```python
def _export_shared(self, facts: list[dict]) -> int:
    self.shared_memory_root.mkdir(parents=True, exist_ok=True)
    export_path = self.shared_memory_root / f"{self.agent_name}_exports.jsonl"
    count = 0
    with export_path.open("a", encoding="utf-8") as f:
        for fact in facts:
            if fact.get("confidence", 0) >= 0.6:
                entry = {
                    "key": fact.get("key", ""),
                    "value": fact.get("value", ""),
                    "source_agent": self.agent_name,
                    "confidence": fact.get("confidence", 0),
                    "timestamp": time.time(),
                    "tags": list(fact.get("entity_tags", [])) if isinstance(fact.get("entity_tags"), (list, tuple)) else [],
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                count += 1
    return count

def _import_shared(self) -> list[dict]:
    if not self.shared_memory_root.exists():
        return []
    imported: list[dict] = []
    existing_keys = {f.get("key") for f in self.memory.search_facts("", limit=self.max_facts * 2, agent_name=self.agent_name)}
    for export_file in self.shared_memory_root.glob("*_exports.jsonl"):
        if export_file.stem.replace("_exports", "") == self.agent_name:
            continue  # skip own exports
        for line in export_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            tags = set(entry.get("tags", []))
            if "personal" in tags and self.agent_name != "alma":
                continue
            if not tags.intersection(self.import_tags):
                continue
            if entry.get("key") in existing_keys:
                continue
            imported.append(entry)
    return imported
```

Add `import json` at top of dream.py (already imported in `_apply_actions`).

Update `run()` to use the new methods:

```python
def run(self) -> DreamResult:
    should, reason = self.should_dream()
    if not should:
        return DreamResult(pruned=0, consolidated=0, duration_seconds=0, skipped=True, reason="conditions_not_met")

    if not _acquire_lock():
        return DreamResult(pruned=0, consolidated=0, duration_seconds=0, skipped=True, reason="lock_held")

    start = time.time()
    try:
        cross_agent_facts = self._import_shared()
        existing_facts = self._orient()
        existing_facts.extend(cross_agent_facts)
        new_signals = self._gather_signal()
        consolidated = self._consolidate(existing_facts, new_signals)
        self._export_shared(existing_facts[:20])  # export top facts
        pruned = self._prune()

        self._last_dream_at = time.time()
        self._sessions_since_dream = 0

        result = DreamResult(
            pruned=pruned,
            consolidated=consolidated,
            duration_seconds=time.time() - start,
        )
        self.observe.emit("auto_dream_complete", payload={
            "agent_name": self.agent_name,
            "pruned": result.pruned,
            "consolidated": result.consolidated,
            "imported": len(cross_agent_facts),
            "duration": result.duration_seconds,
        })
        return result
    except Exception:
        logger.exception("autoDream failed")
        return DreamResult(pruned=0, consolidated=0, duration_seconds=time.time() - start, skipped=True, reason="error")
    finally:
        _release_lock()
```

- [ ] **Step 4: Run dream tests**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_dream.py -x -v`
Expected: All passed

- [ ] **Step 5: Commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add claw_v2/dream.py tests/test_dream.py && git commit -m "feat(dream): add cross-agent export/import with tag filtering and privacy"
```

---

### Task 20: Coordinator with agent awareness

**Files:**
- Modify: `claw_v2/coordinator.py`
- Modify: `tests/test_coordinator.py`

- [ ] **Step 1: Write failing tests**

```python
# Append to tests/test_coordinator.py

class AgentAwareTests(unittest.TestCase):
    def test_worker_task_accepts_assigned_agent(self) -> None:
        task = WorkerTask(name="t1", instruction="fix bug", assigned_agent="hex")
        self.assertEqual(task.assigned_agent, "hex")

    def test_execute_worker_uses_agent_provider_and_model(self) -> None:
        registry = {
            "hex": {"provider": "openai", "model": "gpt-5.3-codex", "soul_text": "You are Hex.", "domains": [], "skills": []},
        }
        svc, router, _, _ = _make_service(agent_registry=registry)
        router.ask.return_value = MagicMock(content="fixed")
        task = WorkerTask(name="fix", instruction="fix the bug", assigned_agent="hex")
        result = svc._execute_worker(task)
        call_kwargs = router.ask.call_args
        self.assertEqual(call_kwargs.kwargs.get("provider"), "openai")
        self.assertEqual(call_kwargs.kwargs.get("model"), "gpt-5.3-codex")
        self.assertEqual(call_kwargs.kwargs.get("system_prompt"), "You are Hex.")

    def test_synthesize_includes_agent_context(self) -> None:
        registry = {
            "hex": {"provider": "openai", "model": "gpt-5.3-codex", "domains": ["code"], "skills": ["bug-triage"]},
        }
        svc, router, _, _ = _make_service(agent_registry=registry)
        router.ask.return_value = MagicMock(content="plan here")
        from claw_v2.coordinator import WorkerResult
        findings = [WorkerResult(task_name="r1", content="found bug", duration_seconds=1.0)]
        result = svc._synthesize("fix bugs", findings)
        prompt_arg = router.ask.call_args.args[0]
        self.assertIn("hex", prompt_arg)
        self.assertIn("code", prompt_arg)
```

Update `_make_service` in test to accept `agent_registry`:

```python
def _make_service(**overrides):
    router = MagicMock()
    observe = MagicMock()
    tmpdir = overrides.pop("scratch_root", None) or Path(tempfile.mkdtemp())
    agent_registry = overrides.pop("agent_registry", None)
    defaults = dict(
        router=router,
        observe=observe,
        scratch_root=tmpdir,
        max_workers=2,
        agent_registry=agent_registry,
    )
    defaults.update(overrides)
    svc = CoordinatorService(**defaults)
    return svc, router, observe, tmpdir
```

- [ ] **Step 2: Run to verify fails**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_coordinator.py::AgentAwareTests -x -v`
Expected: FAIL

- [ ] **Step 3: Update WorkerTask and CoordinatorService**

Add `assigned_agent` to `WorkerTask` in `claw_v2/coordinator.py`:

```python
@dataclass(slots=True)
class WorkerTask:
    name: str
    instruction: str
    lane: str = "research"
    assigned_agent: str | None = None
```

Update `CoordinatorService.__init__`:

```python
def __init__(
    self,
    *,
    router: Any,
    observe: Any,
    scratch_root: Path | str = Path.home() / ".claw" / "scratch",
    max_workers: int = 4,
    agent_registry: dict | None = None,
) -> None:
    self.router = router
    self.observe = observe
    self.scratch_root = Path(scratch_root)
    self.max_workers = max_workers
    self.agent_registry = agent_registry or {}
```

Update `_execute_worker`:

```python
def _execute_worker(self, task: WorkerTask) -> WorkerResult:
    start = time.time()
    try:
        kwargs: dict[str, Any] = {"lane": task.lane, "evidence_pack": {"coordinator_task": task.name}}
        if task.assigned_agent and task.assigned_agent in self.agent_registry:
            agent = self.agent_registry[task.assigned_agent]
            kwargs["provider"] = agent["provider"]
            kwargs["model"] = agent["model"]
            kwargs["system_prompt"] = agent.get("soul_text", "")
        response = self.router.ask(task.instruction, **kwargs)
        return WorkerResult(
            task_name=task.name,
            content=response.content,
            duration_seconds=time.time() - start,
        )
    except Exception as exc:
        return WorkerResult(
            task_name=task.name,
            content="",
            duration_seconds=time.time() - start,
            error=str(exc),
        )
```

Update `_synthesize`:

```python
def _synthesize(self, objective: str, research_results: list[WorkerResult]) -> str:
    findings = "\n\n".join(
        f"### {r.task_name}\n{r.content}" if not r.error
        else f"### {r.task_name}\n[ERROR: {r.error}]"
        for r in research_results
    )

    agent_context = ""
    if self.agent_registry:
        agent_lines = "\n".join(
            f"- {name}: domains={caps.get('domains', [])}, model={caps.get('model', '?')}, skills={caps.get('skills', [])}"
            for name, caps in self.agent_registry.items()
        )
        agent_context = f"\n\n## Available Agents\n{agent_lines}"

    prompt = (
        "You are a coordinator agent. Synthesize the research findings below "
        "into a clear, actionable plan.\n\n"
        f"## Objective\n{objective}{agent_context}\n\n"
        f"## Research Findings\n{findings}\n\n"
        "Output a structured plan with numbered steps. "
        "For each step, assign it to the most appropriate agent based on their domains and skills. "
        "Use the format: **Step N [agent_name]:** description"
    )

    try:
        response = self.router.ask(
            prompt,
            lane="research",
            evidence_pack={"coordinator_phase": "synthesis", "objective": objective},
        )
        return response.content
    except Exception:
        logger.exception("Coordinator synthesis failed")
        return ""
```

- [ ] **Step 4: Run coordinator tests**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_coordinator.py -x -v`
Expected: All passed

- [ ] **Step 5: Commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add claw_v2/coordinator.py tests/test_coordinator.py && git commit -m "feat(coordinator): add agent-aware dispatch with provider/model/system_prompt"
```

---

### Task 21: Ecosystem health metrics

**Files:**
- Create: `claw_v2/ecosystem.py`
- Create: `tests/test_ecosystem.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_ecosystem.py
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.ecosystem import EcosystemHealthService, EcosystemHealth, EcosystemMetric


class CollectTests(unittest.TestCase):
    def test_returns_ok_when_all_healthy(self) -> None:
        bus = MagicMock()
        bus.pending_count.return_value = 0
        bus.pending_urgent.return_value = []
        bus.scan_expired_requests.return_value = []
        observe = MagicMock()
        observe.cost_per_agent_today.return_value = {"hex": 0.1}
        heartbeat = MagicMock()
        heartbeat.collect.return_value = MagicMock(agents={"hex": {"paused": False}})
        svc = EcosystemHealthService(bus=bus, observe=observe, dream_states={}, heartbeat=heartbeat)
        health = svc.collect()
        self.assertEqual(health.overall, "OK")

    def test_bus_lag_warns_on_many_pending(self) -> None:
        bus = MagicMock()
        bus.pending_count.side_effect = lambda name: 5
        bus.pending_urgent.return_value = []
        bus.scan_expired_requests.return_value = []
        observe = MagicMock()
        observe.cost_per_agent_today.return_value = {}
        heartbeat = MagicMock()
        heartbeat.collect.return_value = MagicMock(agents={})
        svc = EcosystemHealthService(bus=bus, observe=observe, dream_states={}, heartbeat=heartbeat)
        health = svc.collect()
        bus_metric = next(m for m in health.metrics if m.name == "bus_lag")
        self.assertEqual(bus_metric.status, "WARN")


class DashboardTests(unittest.TestCase):
    def test_writes_markdown_file(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        dashboard = tmpdir / "ecosystem-health.md"
        bus = MagicMock()
        bus.pending_count.return_value = 0
        bus.pending_urgent.return_value = []
        bus.scan_expired_requests.return_value = []
        observe = MagicMock()
        observe.cost_per_agent_today.return_value = {}
        heartbeat = MagicMock()
        heartbeat.collect.return_value = MagicMock(agents={})
        svc = EcosystemHealthService(bus=bus, observe=observe, dream_states={}, heartbeat=heartbeat)
        svc.write_dashboard(dashboard)
        self.assertTrue(dashboard.exists())
        content = dashboard.read_text()
        self.assertIn("Ecosystem Health", content)
        self.assertIn("Overall", content)
```

- [ ] **Step 2: Run to verify fails**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_ecosystem.py -x -v`
Expected: FAIL — module doesn't exist

- [ ] **Step 3: Implement EcosystemHealthService**

```python
# claw_v2/ecosystem.py
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

KNOWN_AGENTS = ("hex", "rook", "alma", "lux")


@dataclass(slots=True)
class EcosystemMetric:
    name: str
    value: float
    status: Literal["OK", "WARN", "CRITICAL"]
    detail: str


@dataclass(slots=True)
class EcosystemHealth:
    timestamp: float
    metrics: list[EcosystemMetric]
    overall: Literal["OK", "WARN", "CRITICAL"]


class EcosystemHealthService:
    def __init__(
        self,
        *,
        bus: Any,
        observe: Any,
        dream_states: dict[str, Any],
        heartbeat: Any,
    ) -> None:
        self.bus = bus
        self.observe = observe
        self.dream_states = dream_states
        self.heartbeat = heartbeat

    def collect(self) -> EcosystemHealth:
        metrics: list[EcosystemMetric] = []
        metrics.append(self._check_bus_lag())
        metrics.append(self._check_cost_distribution())
        worst = "OK"
        for m in metrics:
            if m.status == "CRITICAL":
                worst = "CRITICAL"
            elif m.status == "WARN" and worst != "CRITICAL":
                worst = "WARN"
        return EcosystemHealth(timestamp=time.time(), metrics=metrics, overall=worst)

    def _check_bus_lag(self) -> EcosystemMetric:
        total = sum(self.bus.pending_count(agent) for agent in KNOWN_AGENTS)
        if total > 10:
            return EcosystemMetric(name="bus_lag", value=total, status="CRITICAL", detail=f"{total} messages pending")
        if total > 3:
            return EcosystemMetric(name="bus_lag", value=total, status="WARN", detail=f"{total} messages pending")
        return EcosystemMetric(name="bus_lag", value=total, status="OK", detail=f"{total} messages pending")

    def _check_cost_distribution(self) -> EcosystemMetric:
        try:
            costs = self.observe.cost_per_agent_today()
            total = sum(costs.values())
            detail = ", ".join(f"{k}:${v:.2f}" for k, v in costs.items()) if costs else "no data"
            return EcosystemMetric(name="cost_distribution", value=total, status="OK", detail=detail)
        except Exception:
            return EcosystemMetric(name="cost_distribution", value=0, status="OK", detail="unavailable")

    def write_dashboard(self, path: Path = Path.home() / ".claw" / "ecosystem-health.md") -> None:
        health = self.collect()
        lines = [
            f"# Ecosystem Health — {time.strftime('%Y-%m-%d %H:%M')}",
            "",
            f"**Overall: {health.overall}**",
            "",
            "| Metric | Value | Status | Detail |",
            "|--------|-------|--------|--------|",
        ]
        for m in health.metrics:
            lines.append(f"| {m.name} | {m.value} | {m.status} | {m.detail} |")
        lines.append("")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
```

- [ ] **Step 4: Run ecosystem tests and full suite**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/test_ecosystem.py -x -v`
Expected: All passed

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/ -x -q --tb=short`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add claw_v2/ecosystem.py tests/test_ecosystem.py && git commit -m "feat(ecosystem): add ecosystem health metrics and dashboard"
```

---

## Final Tasks

### Task 22: Update SOUL.md files with bus topics and Lux model

**Files:**
- Modify: `agents/hex/SOUL.md`
- Modify: `agents/rook/SOUL.md`
- Modify: `agents/alma/SOUL.md`
- Modify: `agents/lux/SOUL.md`

- [ ] **Step 1: Add bus topics section to each SOUL.md**

Append to each agent's SOUL.md:

**Hex:**
```markdown

## Bus Topics
- **Publishes:** `pr_ready`, `tests_fixed`, `dependency_alert`
- **Subscribes:** `test_failure`, `deploy_needed`, `context_bridge`, `security_alert`
```

**Rook:**
```markdown

## Bus Topics
- **Publishes:** `health_critical`, `cron_failure`, `security_alert`, `deploy_complete`
- **Subscribes:** `pr_ready`, `context_bridge`
```

**Alma:**
```markdown

## Bus Topics
- **Publishes:** `user_request`, `context_bridge`, `reminder_due`
- **Subscribes:** all topics (companion sees everything)
```

**Lux — also update model line:**

Change `**Model:** Gemini 3 Pro` to `**Model:** GPT-5.4` and add:

```markdown

## Bus Topics
- **Publishes:** `content_published`, `seo_alert`, `draft_ready`
- **Subscribes:** `deploy_complete`, `context_bridge`, `security_alert`
```

- [ ] **Step 2: Verify Lux model update**

Run: `grep -i "model" /Users/hector/Projects/Dr.-strange/agents/lux/SOUL.md | head -3`
Expected: Contains "GPT-5.4", no mention of "Gemini"

- [ ] **Step 3: Commit**

```bash
cd /Users/hector/Projects/Dr.-strange && git add agents/hex/SOUL.md agents/rook/SOUL.md agents/alma/SOUL.md agents/lux/SOUL.md && git commit -m "feat(agents): add bus topics to SOULs, update Lux model to GPT-5.4"
```

---

### Task 23: Final integration test — full suite

- [ ] **Step 1: Run the complete test suite**

Run: `cd /Users/hector/Projects/Dr.-strange && python -m pytest tests/ -v --tb=short`
Expected: All tests pass, including new tests for bus, kairos, heartbeat registry, memory scoped, dream export/import, coordinator agent-aware, ecosystem.

- [ ] **Step 2: Verify file counts match spec**

Run: `find agents/*/skills/*/SKILL.md | wc -l`
Expected: 16 total (4 hex + 4 rook + 4 alma + ~4 lux — verify Lux has its existing 10+)

Run: `ls claw_v2/bus.py claw_v2/ecosystem.py`
Expected: Both exist

- [ ] **Step 3: Final commit if any loose changes**

```bash
cd /Users/hector/Projects/Dr.-strange && git status
```
