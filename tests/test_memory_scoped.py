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

    def test_migration_on_existing_db(self) -> None:
        """Opening a second MemoryStore on same DB should not fail."""
        store2 = MemoryStore(self.db_path)
        store2.store_fact("test", "val", source="test", agent_name="alma")
        facts = store2.search_facts("test", agent_name="alma")
        self.assertEqual(len(facts), 1)


class ProviderSessionTTLTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = Path(tempfile.mkdtemp()) / "test.db"
        self.store = MemoryStore(self.db_path)

    def test_fresh_session_is_returned(self) -> None:
        self.store.link_provider_session("app-1", "anthropic", "sdk-abc")
        result = self.store.get_provider_session("app-1", "anthropic")
        self.assertEqual(result, "sdk-abc")

    def test_stale_session_is_expired(self) -> None:
        self.store.link_provider_session("app-1", "anthropic", "sdk-old")
        # Backdate the updated_at to 3 hours ago
        self.store._conn.execute(
            "UPDATE provider_sessions SET updated_at = datetime('now', '-3 hours') WHERE app_session_id = 'app-1'"
        )
        self.store._conn.commit()
        result = self.store.get_provider_session("app-1", "anthropic", max_age_seconds=7200)
        self.assertIsNone(result)
        # Confirm the row was cleaned up
        row = self.store._conn.execute(
            "SELECT * FROM provider_sessions WHERE app_session_id = 'app-1'"
        ).fetchone()
        self.assertIsNone(row)

    def test_custom_ttl_is_respected(self) -> None:
        self.store.link_provider_session("app-1", "anthropic", "sdk-recent")
        # 10 minutes old
        self.store._conn.execute(
            "UPDATE provider_sessions SET updated_at = datetime('now', '-10 minutes') WHERE app_session_id = 'app-1'"
        )
        self.store._conn.commit()
        # With 1-hour TTL, should still be valid
        result = self.store.get_provider_session("app-1", "anthropic", max_age_seconds=3600)
        self.assertEqual(result, "sdk-recent")
        # With 5-minute TTL, should be expired
        result = self.store.get_provider_session("app-1", "anthropic", max_age_seconds=300)
        self.assertIsNone(result)

    def test_link_provider_session_tracks_last_message_id(self) -> None:
        self.store.store_message("app-1", "user", "hello")
        self.store.store_message("app-1", "assistant", "world")
        self.store.link_provider_session("app-1", "anthropic", "sdk-1")

        self.assertEqual(self.store.get_provider_session("app-1", "anthropic"), "sdk-1")
        self.assertEqual(self.store.get_provider_session_cursor("app-1", "anthropic"), 2)

    def test_get_messages_since_returns_only_unsynced_messages(self) -> None:
        self.store.store_message("app-1", "user", "synced-user")
        self.store.store_message("app-1", "assistant", "synced-assistant")
        self.store.link_provider_session("app-1", "anthropic", "sdk-1")
        self.store.store_message("app-1", "user", "shortcut-user")
        self.store.store_message("app-1", "assistant", "shortcut-assistant")

        recent = self.store.get_messages_since("app-1", 2)
        self.assertEqual(
            [(row["role"], row["content"]) for row in recent],
            [("user", "shortcut-user"), ("assistant", "shortcut-assistant")],
        )

    def test_replace_latest_assistant_message_updates_visible_reply(self) -> None:
        self.store.store_message("app-1", "user", "hello")
        self.store.store_message("app-1", "assistant", "(no result)")

        replaced = self.store.replace_latest_assistant_message(
            "app-1",
            "(no result)",
            "Recibido. ¿Qué quieres que haga con esto?",
        )

        self.assertTrue(replaced)
        recent = self.store.get_recent_messages("app-1", limit=2)
        self.assertEqual(recent[-1]["content"], "Recibido. ¿Qué quieres que haga con esto?")
