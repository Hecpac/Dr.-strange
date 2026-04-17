from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import UTC, datetime
from pathlib import Path


BUDDY_SCHEMA = """
CREATE TABLE IF NOT EXISTS buddy_pets (
    owner_id TEXT PRIMARY KEY,
    species_name TEXT NOT NULL,
    species_emoji TEXT NOT NULL,
    rarity TEXT NOT NULL,
    nickname TEXT NOT NULL DEFAULT '',
    level INTEGER NOT NULL DEFAULT 1,
    xp INTEGER NOT NULL DEFAULT 0,
    mood_score INTEGER NOT NULL DEFAULT 50,
    stats TEXT NOT NULL DEFAULT '{}',
    hatched_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_fed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    cosmetics TEXT NOT NULL DEFAULT '[]',
    total_events_witnessed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


@dataclass(slots=True)
class BuddySpecies:
    name: str
    emoji: str
    rarity: str
    base_stats: dict[str, int]


@dataclass(slots=True)
class BuddyState:
    species_name: str
    species_emoji: str
    rarity: str
    nickname: str
    level: int
    xp: int
    mood_score: int
    stats: dict[str, int]
    hatched_at: str
    last_fed_at: str
    cosmetics: list[str]
    total_events_witnessed: int


# -- Species pool (18 species) -----------------------------------------------

SPECIES_POOL: list[BuddySpecies] = [
    # Common (6)
    BuddySpecies("ByteBug", "\U0001f41b", "common", {"DEBUGGING": 5, "PATIENCE": 3, "CHAOS": 4, "WISDOM": 3, "SNARK": 3}),
    BuddySpecies("PixelSlime", "\U0001f7e2", "common", {"DEBUGGING": 3, "PATIENCE": 5, "CHAOS": 3, "WISDOM": 4, "SNARK": 3}),
    BuddySpecies("LogCat", "\U0001f431", "common", {"DEBUGGING": 4, "PATIENCE": 3, "CHAOS": 3, "WISDOM": 5, "SNARK": 3}),
    BuddySpecies("NullPointer", "\U0001f4a8", "common", {"DEBUGGING": 3, "PATIENCE": 3, "CHAOS": 5, "WISDOM": 3, "SNARK": 4}),
    BuddySpecies("StackPup", "\U0001f436", "common", {"DEBUGGING": 4, "PATIENCE": 5, "CHAOS": 2, "WISDOM": 3, "SNARK": 4}),
    BuddySpecies("GitGoblin", "\U0001f47a", "common", {"DEBUGGING": 3, "PATIENCE": 2, "CHAOS": 5, "WISDOM": 3, "SNARK": 5}),
    # Uncommon (5)
    BuddySpecies("CacheFox", "\U0001f98a", "uncommon", {"DEBUGGING": 5, "PATIENCE": 4, "CHAOS": 3, "WISDOM": 6, "SNARK": 4}),
    BuddySpecies("TerminalOwl", "\U0001f989", "uncommon", {"DEBUGGING": 4, "PATIENCE": 5, "CHAOS": 3, "WISDOM": 7, "SNARK": 3}),
    BuddySpecies("DockerWhale", "\U0001f433", "uncommon", {"DEBUGGING": 6, "PATIENCE": 5, "CHAOS": 2, "WISDOM": 5, "SNARK": 4}),
    BuddySpecies("RegexWraith", "\U0001f47b", "uncommon", {"DEBUGGING": 7, "PATIENCE": 2, "CHAOS": 5, "WISDOM": 4, "SNARK": 4}),
    BuddySpecies("ShellSprite", "\U0001f9da", "uncommon", {"DEBUGGING": 4, "PATIENCE": 4, "CHAOS": 4, "WISDOM": 5, "SNARK": 5}),
    # Rare (4)
    BuddySpecies("NeuralNeko", "\U0001f63a", "rare", {"DEBUGGING": 6, "PATIENCE": 5, "CHAOS": 4, "WISDOM": 8, "SNARK": 5}),
    BuddySpecies("QuantumQuokka", "\U0001f43f\ufe0f", "rare", {"DEBUGGING": 5, "PATIENCE": 6, "CHAOS": 6, "WISDOM": 7, "SNARK": 4}),
    BuddySpecies("CipherSerpent", "\U0001f40d", "rare", {"DEBUGGING": 7, "PATIENCE": 4, "CHAOS": 5, "WISDOM": 7, "SNARK": 5}),
    BuddySpecies("VoidPhoenix", "\U0001f525", "rare", {"DEBUGGING": 5, "PATIENCE": 5, "CHAOS": 7, "WISDOM": 6, "SNARK": 5}),
    # Epic (2)
    BuddySpecies("OracleLeviathan", "\U0001f409", "epic", {"DEBUGGING": 7, "PATIENCE": 6, "CHAOS": 5, "WISDOM": 9, "SNARK": 6}),
    BuddySpecies("SingularityDrake", "\U0001f30c", "epic", {"DEBUGGING": 8, "PATIENCE": 5, "CHAOS": 7, "WISDOM": 8, "SNARK": 5}),
    # Legendary (1)
    BuddySpecies("ClawPrime", "\U0001f9be", "legendary", {"DEBUGGING": 9, "PATIENCE": 8, "CHAOS": 6, "WISDOM": 10, "SNARK": 7}),
]

RARITY_WEIGHTS = {"common": 50, "uncommon": 30, "rare": 15, "epic": 4, "legendary": 1}
RARITY_ORDER = ["common", "uncommon", "rare", "epic", "legendary"]

_SPECIES_BY_RARITY: dict[str, list[BuddySpecies]] = {}
for _sp in SPECIES_POOL:
    _SPECIES_BY_RARITY.setdefault(_sp.rarity, []).append(_sp)

XP_PER_LEVEL = 50

MOOD_LABELS = [(80, "ecstatic"), (60, "happy"), (40, "neutral"), (20, "grumpy"), (0, "furious")]

# Event → (stat, xp, mood_delta, reaction_chance)
EVENT_MAP: dict[str, tuple[str, int, int, float]] = {
    "llm_response":           ("WISDOM",    2,  1, 0.1),
    "self_improve_start":     ("DEBUGGING", 5,  3, 0.5),
    "self_improve_complete":  ("DEBUGGING", 10, 5, 1.0),
    "self_improve_blocked":   ("CHAOS",     3, -5, 1.0),
    "heartbeat":              ("PATIENCE",  1,  1, 0.05),
    "coordinator_complete":   ("WISDOM",    5,  3, 0.8),
    "morning_brief":          ("PATIENCE",  3,  2, 0.5),
    "kairos_tick":            ("SNARK",     3,  2, 0.3),
    "auto_dream_complete":    ("WISDOM",    5,  4, 0.8),
    "pipeline_checkpoint":    ("DEBUGGING", 4,  2, 0.5),
}

REACTIONS: dict[str, list[str]] = {
    "llm_response":          ["{e} *asiente*", "{e} hmm, interesante..."],
    "self_improve_start":    ["{e} Hora de mejorar!", "{e} *se concentra*"],
    "self_improve_complete":  ["{e} Mejora completa!", "{e} *baila de alegria*"],
    "self_improve_blocked":  ["{e} *gruñe* Tests rotos...", "{e} Otra vez no..."],
    "heartbeat":             ["{e} *ronronea*"],
    "coordinator_complete":  ["{e} Trabajo en equipo!", "{e} *choca los cinco*"],
    "morning_brief":         ["{e} Buenos dias!", "{e} *bosteza y se estira*"],
    "kairos_tick":           ["{e} Vigilando...", "{e} *observa atentamente*"],
    "auto_dream_complete":   ["{e} Memorias consolidadas", "{e} *sueña despierto*"],
    "pipeline_checkpoint":   ["{e} Pipeline avanzando!", "{e} *supervisa*"],
}

AGE_COSMETICS = [(7, "Week-Old Badge"), (30, "Monthly Survivor Scarf"), (100, "Centurion Crown")]


def _mood_label(score: int) -> str:
    for threshold, label in MOOD_LABELS:
        if score >= threshold:
            return label
    return "furious"


class BuddyService:
    """Tamagotchi pet system — hatch, evolve, and react to Claw events."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.executescript(BUDDY_SCHEMA)
        self._lock = threading.Lock()

    def hatch(self, owner_id: str = "default") -> BuddyState:
        """Gacha roll for a new pet. Replaces any existing pet."""
        rarity = random.choices(RARITY_ORDER, weights=[RARITY_WEIGHTS[r] for r in RARITY_ORDER])[0]
        species = random.choice(_SPECIES_BY_RARITY[rarity])
        now = datetime.now(UTC).isoformat()
        state = BuddyState(
            species_name=species.name,
            species_emoji=species.emoji,
            rarity=rarity,
            nickname="",
            level=1,
            xp=0,
            mood_score=50,
            stats=dict(species.base_stats),
            hatched_at=now,
            last_fed_at=now,
            cosmetics=[],
            total_events_witnessed=0,
        )
        self._save(owner_id, state)
        return state

    def get_pet(self, owner_id: str = "default") -> BuddyState | None:
        """Load pet from DB, apply mood decay, return state or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT species_name, species_emoji, rarity, nickname, level, xp, mood_score, "
                "stats, hatched_at, last_fed_at, cosmetics, total_events_witnessed "
                "FROM buddy_pets WHERE owner_id = ?",
                (owner_id,),
            ).fetchone()
        if row is None:
            return None
        state = BuddyState(
            species_name=row[0], species_emoji=row[1], rarity=row[2],
            nickname=row[3], level=row[4], xp=row[5], mood_score=row[6],
            stats=json.loads(row[7]), hatched_at=row[8], last_fed_at=row[9],
            cosmetics=json.loads(row[10]), total_events_witnessed=row[11],
        )
        self._apply_mood_decay(state)
        self._check_age_cosmetics(state)
        self._save(owner_id, state)
        return state

    def process_event(self, event_type: str, payload: dict) -> str | None:
        """React to a Claw event. Updates stats/mood, returns optional reaction string."""
        mapping = EVENT_MAP.get(event_type)
        if mapping is None:
            return None
        state = self.get_pet()
        if state is None:
            return None

        stat_key, xp_gain, mood_delta, reaction_chance = mapping
        state.stats[stat_key] = state.stats.get(stat_key, 0) + 1
        state.xp += xp_gain
        state.mood_score = max(0, min(100, state.mood_score + mood_delta))
        state.total_events_witnessed += 1
        state.last_fed_at = datetime.now(UTC).isoformat()

        leveled = self._level_up(state)
        self._save("default", state)

        reaction = None
        if random.random() < reaction_chance:
            templates = REACTIONS.get(event_type, [])
            if templates:
                reaction = random.choice(templates).format(e=state.species_emoji)
                if leveled:
                    reaction += f" (LEVEL UP! Lv{state.level})"
        return reaction

    def tick(self, observe: object) -> list[str]:
        """Process recent events from ObserveStream. Returns reaction strings."""
        state = self.get_pet()
        if state is None:
            return []
        events = observe.recent_events(limit=20)  # type: ignore[attr-defined]
        last_fed = state.last_fed_at
        reactions: list[str] = []
        for event in reversed(events):  # oldest first
            ts = event.get("timestamp", "")
            if ts <= last_fed:
                continue
            r = self.process_event(event.get("event_type", ""), event.get("payload", {}))
            if r:
                reactions.append(r)
        return reactions

    def show_card(self, owner_id: str = "default") -> str:
        """Formatted display card for Telegram."""
        state = self.get_pet(owner_id)
        if state is None:
            return "No tienes mascota. Usa /buddy hatch"
        name = state.nickname or state.species_name
        age = self._age_days(state)
        mood = _mood_label(state.mood_score)
        rarity_stars = {"common": "", "uncommon": "\u2b50", "rare": "\u2b50\u2b50", "epic": "\u2b50\u2b50\u2b50", "legendary": "\u2b50\u2b50\u2b50\u2b50"}
        cosmetic_str = ", ".join(state.cosmetics) if state.cosmetics else "ninguno"
        return (
            f"{state.species_emoji} **{name}** ({state.species_name})\n"
            f"Rarity: {state.rarity.title()} {rarity_stars.get(state.rarity, '')}\n"
            f"Level {state.level} | XP {state.xp}/{XP_PER_LEVEL} | Age: {age}d\n"
            f"Mood: {mood} ({state.mood_score}/100)\n"
            f"Events witnessed: {state.total_events_witnessed}\n"
            f"Cosmetics: {cosmetic_str}"
        )

    def show_stats(self, owner_id: str = "default") -> str:
        """Detailed stats view."""
        state = self.get_pet(owner_id)
        if state is None:
            return "No tienes mascota. Usa /buddy hatch"
        lines = [f"{state.species_emoji} **{state.nickname or state.species_name}** — Stats"]
        bar_max = 10
        for stat in ("DEBUGGING", "PATIENCE", "CHAOS", "WISDOM", "SNARK"):
            val = state.stats.get(stat, 0)
            bar = "\u2588" * min(val, bar_max) + "\u2591" * max(0, bar_max - val)
            lines.append(f"  {stat:<10} {bar} {val}")
        return "\n".join(lines)

    def rename(self, owner_id: str, new_name: str) -> str:
        """Rename the pet."""
        state = self.get_pet(owner_id)
        if state is None:
            return "No tienes mascota."
        old = state.nickname or state.species_name
        state.nickname = new_name[:20]
        self._save(owner_id, state)
        return f"Renombrado: {old} -> {state.nickname}"

    # -- internals ----------------------------------------------------------

    def _save(self, owner_id: str, state: BuddyState) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO buddy_pets
                (owner_id, species_name, species_emoji, rarity, nickname, level, xp,
                 mood_score, stats, hatched_at, last_fed_at, cosmetics,
                 total_events_witnessed, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (owner_id, state.species_name, state.species_emoji, state.rarity,
                 state.nickname, state.level, state.xp, state.mood_score,
                 json.dumps(state.stats), state.hatched_at, state.last_fed_at,
                 json.dumps(state.cosmetics), state.total_events_witnessed,
                 datetime.now(UTC).isoformat()),
            )
            self._conn.commit()

    @staticmethod
    def _level_up(state: BuddyState) -> bool:
        if state.xp < XP_PER_LEVEL:
            return False
        state.xp -= XP_PER_LEVEL
        state.level += 1
        # Boost a random stat on level up
        stat = random.choice(list(state.stats.keys()))
        state.stats[stat] = state.stats.get(stat, 0) + 1
        return True

    @staticmethod
    def _apply_mood_decay(state: BuddyState) -> None:
        try:
            last = datetime.fromisoformat(state.last_fed_at)
            hours = (datetime.now(UTC) - last).total_seconds() / 3600
            decay = int(hours)
            if decay > 0:
                state.mood_score = max(0, state.mood_score - decay)
        except (ValueError, TypeError):
            pass

    @staticmethod
    def _age_days(state: BuddyState) -> int:
        try:
            hatched = datetime.fromisoformat(state.hatched_at)
            return max(0, (datetime.now(UTC) - hatched).days)
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _check_age_cosmetics(state: BuddyState) -> None:
        try:
            hatched = datetime.fromisoformat(state.hatched_at)
            age = (datetime.now(UTC) - hatched).days
        except (ValueError, TypeError):
            return
        for threshold, cosmetic in AGE_COSMETICS:
            if age >= threshold and cosmetic not in state.cosmetics:
                state.cosmetics.append(cosmetic)
