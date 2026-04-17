from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.buddy import (
    SPECIES_POOL,
    BuddyService,
    BuddyState,
    XP_PER_LEVEL,
    _SPECIES_BY_RARITY,
    _mood_label,
)


def _make_service(tmpdir: str | None = None) -> tuple[BuddyService, Path]:
    root = Path(tmpdir or tempfile.mkdtemp())
    db = root / "test.db"
    return BuddyService(db), db


class SpeciesPoolTests(unittest.TestCase):
    def test_pool_has_18_species(self) -> None:
        self.assertEqual(len(SPECIES_POOL), 18)

    def test_rarity_distribution(self) -> None:
        counts = Counter(s.rarity for s in SPECIES_POOL)
        self.assertEqual(counts["common"], 6)
        self.assertEqual(counts["uncommon"], 5)
        self.assertEqual(counts["rare"], 4)
        self.assertEqual(counts["epic"], 2)
        self.assertEqual(counts["legendary"], 1)

    def test_all_species_have_5_stats(self) -> None:
        for sp in SPECIES_POOL:
            self.assertEqual(set(sp.base_stats.keys()), {"DEBUGGING", "PATIENCE", "CHAOS", "WISDOM", "SNARK"})


class HatchTests(unittest.TestCase):
    def test_hatch_creates_pet(self) -> None:
        svc, _ = _make_service()
        state = svc.hatch()
        self.assertIsInstance(state, BuddyState)
        self.assertEqual(state.level, 1)
        self.assertEqual(state.xp, 0)
        self.assertIn(state.rarity, ["common", "uncommon", "rare", "epic", "legendary"])

    def test_hatch_replaces_existing(self) -> None:
        svc, _ = _make_service()
        first = svc.hatch()
        second = svc.hatch()
        pet = svc.get_pet()
        self.assertEqual(pet.hatched_at, second.hatched_at)

    def test_get_pet_returns_none_when_empty(self) -> None:
        svc, _ = _make_service()
        self.assertIsNone(svc.get_pet())


class GachaTests(unittest.TestCase):
    def test_distribution_roughly_correct(self) -> None:
        svc, _ = _make_service()
        counts: Counter = Counter()
        for _ in range(5000):
            state = svc.hatch()
            counts[state.rarity] += 1
        self.assertGreater(counts["common"], counts["uncommon"])
        self.assertGreater(counts["uncommon"], counts["rare"])
        self.assertGreater(counts["rare"], counts["epic"])


class ProcessEventTests(unittest.TestCase):
    def test_event_increases_stat_and_xp(self) -> None:
        svc, _ = _make_service()
        svc.hatch()
        old = svc.get_pet()
        old_wisdom = old.stats["WISDOM"]
        old_xp = old.xp
        svc.process_event("llm_response", {})
        new = svc.get_pet()
        self.assertGreater(new.stats["WISDOM"], old_wisdom)
        self.assertGreater(new.xp, old_xp)

    def test_unknown_event_returns_none(self) -> None:
        svc, _ = _make_service()
        svc.hatch()
        self.assertIsNone(svc.process_event("unknown_event_xyz", {}))

    def test_no_pet_returns_none(self) -> None:
        svc, _ = _make_service()
        self.assertIsNone(svc.process_event("llm_response", {}))

    def test_negative_mood_clamped(self) -> None:
        svc, _ = _make_service()
        state = svc.hatch()
        state.mood_score = 1
        svc._save("default", state)
        svc.process_event("self_improve_blocked", {})
        pet = svc.get_pet()
        self.assertGreaterEqual(pet.mood_score, 0)


class LevelUpTests(unittest.TestCase):
    def test_level_up_at_threshold(self) -> None:
        svc, _ = _make_service()
        state = svc.hatch()
        state.xp = XP_PER_LEVEL - 1
        svc._save("default", state)
        # llm_response gives +2 XP
        svc.process_event("llm_response", {})
        pet = svc.get_pet()
        self.assertEqual(pet.level, 2)


class MoodDecayTests(unittest.TestCase):
    def test_mood_decays_over_time(self) -> None:
        svc, _ = _make_service()
        state = svc.hatch()
        state.mood_score = 80
        state.last_fed_at = (datetime.now(UTC) - timedelta(hours=10)).isoformat()
        svc._save("default", state)
        pet = svc.get_pet()
        self.assertLess(pet.mood_score, 80)

    def test_mood_label(self) -> None:
        self.assertEqual(_mood_label(90), "ecstatic")
        self.assertEqual(_mood_label(70), "happy")
        self.assertEqual(_mood_label(50), "neutral")
        self.assertEqual(_mood_label(30), "grumpy")
        self.assertEqual(_mood_label(10), "furious")


class DisplayTests(unittest.TestCase):
    def test_show_card_contains_info(self) -> None:
        svc, _ = _make_service()
        svc.hatch()
        card = svc.show_card()
        self.assertIn("Level", card)
        self.assertIn("Mood", card)

    def test_show_stats_contains_all_stats(self) -> None:
        svc, _ = _make_service()
        svc.hatch()
        stats = svc.show_stats()
        for s in ("DEBUGGING", "PATIENCE", "CHAOS", "WISDOM", "SNARK"):
            self.assertIn(s, stats)

    def test_show_card_no_pet(self) -> None:
        svc, _ = _make_service()
        self.assertIn("hatch", svc.show_card())


class RenameTests(unittest.TestCase):
    def test_rename_pet(self) -> None:
        svc, _ = _make_service()
        svc.hatch()
        svc.rename("default", "Buddy Jr")
        pet = svc.get_pet()
        self.assertEqual(pet.nickname, "Buddy Jr")

    def test_rename_no_pet(self) -> None:
        svc, _ = _make_service()
        result = svc.rename("default", "x")
        self.assertIn("No tienes", result)


class PersistenceTests(unittest.TestCase):
    def test_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            svc1, db = _make_service(tmpdir)
            state = svc1.hatch()
            svc2 = BuddyService(db)
            pet = svc2.get_pet()
            self.assertEqual(pet.species_name, state.species_name)


class TickTests(unittest.TestCase):
    def test_tick_processes_events(self) -> None:
        svc, _ = _make_service()
        svc.hatch()
        observe = MagicMock()
        past = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
        future = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
        observe.recent_events.return_value = [
            {"event_type": "self_improve_complete", "payload": {}, "timestamp": future},
        ]
        # Set last_fed_at to past so the event is "new"
        state = svc.get_pet()
        state.last_fed_at = past
        svc._save("default", state)
        reactions = svc.tick(observe)
        pet = svc.get_pet()
        self.assertGreater(pet.total_events_witnessed, 0)


class AgeCosmetics(unittest.TestCase):
    def test_cosmetic_at_30_days(self) -> None:
        svc, _ = _make_service()
        state = svc.hatch()
        state.hatched_at = (datetime.now(UTC) - timedelta(days=31)).isoformat()
        svc._save("default", state)
        pet = svc.get_pet()
        self.assertIn("Monthly Survivor Scarf", pet.cosmetics)


if __name__ == "__main__":
    unittest.main()
