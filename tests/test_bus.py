from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path

from claw_v2.bus import AgentBus, AgentMessage, _new_message


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
        data = json.loads(json.dumps(asdict(msg)))
        restored = AgentMessage(**data)
        self.assertEqual(restored.id, msg.id)
        self.assertEqual(restored.payload, msg.payload)
        self.assertIsNone(restored.to_agent)


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
        self.bus.send(msg)
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
        hex_inbox = self.tmpdir / "inbox" / "hex"
        self.assertEqual(len(list(hex_inbox.glob("*.json"))), 0)
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
        self.assertEqual(len(list((self.tmpdir / "inbox" / "hex").glob("*.json"))), 0)
        self.assertEqual(len(list((self.tmpdir / "archive").glob("*.json"))), 1)

    def test_receive_skips_expired_messages(self) -> None:
        msg = _new_message(from_agent="rook", to_agent="hex", intent="notify", topic="old", payload={}, ttl_seconds=1)
        msg.created_at = time.time() - 10
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
        self.bus.reply(received[0], content={"answer": "done"}, from_agent="hex")
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
        self.bus.receive("hex")
        archive_files = list((self.tmpdir / "archive").glob("*.json"))
        self.assertEqual(len(archive_files), 1)
        old_time = time.time() - (8 * 86400)
        os.utime(archive_files[0], (old_time, old_time))
        removed = self.bus.cleanup(max_age_days=7)
        self.assertEqual(removed, 1)
        self.assertEqual(len(list((self.tmpdir / "archive").glob("*.json"))), 0)
