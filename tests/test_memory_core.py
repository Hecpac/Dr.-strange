"""Tests for core MemoryStore operations: messages, facts, delete, build_context with history."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from claw_v2.memory import MemoryStore


class MessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore(Path(tempfile.mkdtemp()) / "test.db")

    def test_store_and_retrieve_messages(self) -> None:
        self.store.store_message("s1", "user", "hola")
        self.store.store_message("s1", "assistant", "dime")
        msgs = self.store.get_recent_messages("s1", limit=10)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[1]["content"], "dime")

    def test_messages_ordered_chronologically(self) -> None:
        for i in range(5):
            self.store.store_message("s1", "user", f"msg-{i}")
        msgs = self.store.get_recent_messages("s1", limit=10)
        self.assertEqual([m["content"] for m in msgs], [f"msg-{i}" for i in range(5)])

    def test_limit_truncates_oldest(self) -> None:
        for i in range(10):
            self.store.store_message("s1", "user", f"msg-{i}")
        msgs = self.store.get_recent_messages("s1", limit=3)
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[0]["content"], "msg-7")

    def test_sessions_are_isolated(self) -> None:
        self.store.store_message("s1", "user", "a")
        self.store.store_message("s2", "user", "b")
        self.assertEqual(len(self.store.get_recent_messages("s1")), 1)
        self.assertEqual(len(self.store.get_recent_messages("s2")), 1)

    def test_last_message_id(self) -> None:
        self.assertEqual(self.store.last_message_id("s1"), 0)
        self.store.store_message("s1", "user", "hi")
        self.assertGreater(self.store.last_message_id("s1"), 0)


class DeleteFactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore(Path(tempfile.mkdtemp()) / "test.db")

    def test_delete_existing_fact(self) -> None:
        self.store.store_fact("temp", "val", source="test")
        self.assertTrue(self.store.delete_fact("temp"))
        self.assertEqual(self.store.search_facts("temp"), [])

    def test_delete_nonexistent_fact_returns_false(self) -> None:
        self.assertFalse(self.store.delete_fact("nope"))

    def test_delete_only_removes_target(self) -> None:
        self.store.store_fact("keep", "v1", source="test")
        self.store.store_fact("drop", "v2", source="test")
        self.store.delete_fact("drop")
        self.assertEqual(len(self.store.search_facts("keep")), 1)


class BuildContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore(Path(tempfile.mkdtemp()) / "test.db")

    def test_context_includes_current_input(self) -> None:
        ctx = self.store.build_context("s1", message="hazlo")
        self.assertIn("# Current input", ctx)
        self.assertIn("hazlo", ctx)

    def test_context_includes_history_when_enabled(self) -> None:
        self.store.store_message("s1", "user", "paso 1")
        self.store.store_message("s1", "assistant", "hecho")
        ctx = self.store.build_context("s1", message="paso 2", include_history=True)
        self.assertIn("paso 1", ctx)

    def test_context_excludes_history_when_disabled(self) -> None:
        self.store.store_message("s1", "user", "viejo")
        self.store.store_message("s1", "assistant", "resp")
        ctx = self.store.build_context("s1", message="nuevo", include_history=False)
        self.assertNotIn("viejo", ctx)

    def test_context_includes_session_state(self) -> None:
        self.store.update_session_state("s1", mode="coding", current_goal="fix bug")
        ctx = self.store.build_context("s1", message="go")
        self.assertIn("mode=coding", ctx)
        self.assertIn("current_goal=fix bug", ctx)


class SessionStateLockTests(unittest.TestCase):
    """Verify the TOCTOU fix: concurrent update_session_state calls don't lose data."""

    def setUp(self) -> None:
        self.store = MemoryStore(Path(tempfile.mkdtemp()) / "test.db")

    def test_concurrent_updates_dont_corrupt(self) -> None:
        errors: list[Exception] = []

        def writer(goal: str) -> None:
            try:
                for _ in range(20):
                    self.store.update_session_state("s1", current_goal=goal)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(f"goal-{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        state = self.store.get_session_state("s1")
        self.assertTrue(state["current_goal"].startswith("goal-"))


if __name__ == "__main__":
    unittest.main()
