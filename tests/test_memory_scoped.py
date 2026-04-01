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
