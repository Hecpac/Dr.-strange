"""P0-F: LearningLoop must deduplicate identical soul_update_suggestion proposals.

Behavioral audit found 6+ identical `soul_update_suggestion.<ts>` facts
written over 4 consecutive days (always proposing the same rule). The
timestamp in the key forces every run to insert a fresh fact, so the
suggestion never converges and the memory layer becomes saturated with
meta-routing duplicates.

Fix contract:
  - Use content_hash (not wall-clock timestamp) for the key suffix, so
    the same proposal yields the same key.
  - When a fact with that key already exists, do NOT insert again —
    instead bump confidence (capped at 1.0) and emit
    `learning_loop_dedup`.
  - When the key is new, behave as before (insert + emit
    `soul_update_suggestion`).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.learning import LearningLoop
from claw_v2.memory import MemoryStore


class _RecordingObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **kwargs: object) -> None:
        self.events.append((event_type, dict(kwargs)))

    def recent_events(self, *, limit: int = 100):  # type: ignore[override]
        return []


def _count_soul_suggestion_facts(memory: MemoryStore) -> int:
    rows = memory._conn.execute(  # type: ignore[attr-defined]
        "SELECT COUNT(*) AS c FROM facts WHERE key LIKE 'soul_update_suggestion.%'"
    ).fetchone()
    return int(rows[0]) if rows else 0


def _confidence_for_key(memory: MemoryStore, key: str) -> float:
    row = memory._conn.execute(  # type: ignore[attr-defined]
        "SELECT confidence FROM facts WHERE key = ? LIMIT 1", (key,)
    ).fetchone()
    return float(row[0]) if row else 0.0


_FIXED_PROPOSAL = {
    "summary": "Route short conversational Spanish continuations through brain",
    "suggestions": [
        {
            "rule": "Brain owns short Spanish continuations",
            "rationale": "Repeated outcomes show brain handles continuations best.",
        }
    ],
}


class LearningLoopDedupTests(unittest.TestCase):
    def _build(self) -> tuple[Path, MemoryStore, LearningLoop, _RecordingObserve]:
        tmp = Path(tempfile.mkdtemp())
        memory = MemoryStore(tmp / "m.db")
        observe = _RecordingObserve()
        loop = LearningLoop(memory=memory, observe=observe)
        # `_derive_soul_update_proposal` is patched in each test via patch.object
        # because LearningLoop is a slots dataclass and does not allow direct
        # attribute assignment.
        return tmp, memory, loop, observe

    def _patched(self, loop: LearningLoop):
        return patch.object(
            LearningLoop,
            "_derive_soul_update_proposal",
            new=lambda *_, **__: dict(_FIXED_PROPOSAL),
        )

    def test_duplicate_soul_update_suggestion_is_consolidated(self) -> None:
        _, memory, loop, observe = self._build()
        ctx = self._patched(loop)
        ctx.start()
        self.addCleanup(ctx.stop)
        first = loop.suggest_soul_updates(
            observe=observe,
            soul_text="role: dr-strange",
            min_signals=0,
        )
        self.assertIsNotNone(first, "first suggest_soul_updates should produce a proposal")
        second = loop.suggest_soul_updates(
            observe=observe,
            soul_text="role: dr-strange",
            min_signals=0,
        )
        self.assertIsNotNone(second, "second call must still return a proposal even on dedup")

        # Exactly one persistent fact, not two.
        self.assertEqual(
            _count_soul_suggestion_facts(memory),
            1,
            "duplicate soul_update_suggestion must not create a second fact",
        )

        event_types = [e[0] for e in observe.events]
        self.assertIn(
            "soul_update_suggestion", event_types,
            "first persisted proposal still emits the canonical event",
        )
        self.assertIn(
            "learning_loop_dedup", event_types,
            "second (duplicate) call must emit a learning_loop_dedup event",
        )

    def test_dedup_uses_content_hash_in_key(self) -> None:
        _, memory, loop, observe = self._build()
        ctx = self._patched(loop)
        ctx.start()
        self.addCleanup(ctx.stop)
        loop.suggest_soul_updates(
            observe=observe,
            soul_text="role: dr-strange",
            min_signals=0,
        )
        rows = memory._conn.execute(  # type: ignore[attr-defined]
            "SELECT key FROM facts WHERE key LIKE 'soul_update_suggestion.%'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        key = rows[0][0]
        # Suffix should be a content hash, not a wall-clock timestamp.
        # Timestamps from int(time.time()) are 10+ digit decimal integers;
        # we want a hex digest of at least 8 chars and non-numeric.
        suffix = key.split(".", 1)[1]
        self.assertGreaterEqual(len(suffix), 8)
        self.assertFalse(suffix.isdigit(), f"key suffix should not be a raw timestamp: {key!r}")

    def test_dedup_bumps_confidence_capped_at_one(self) -> None:
        _, memory, loop, observe = self._build()
        ctx = self._patched(loop)
        ctx.start()
        self.addCleanup(ctx.stop)
        loop.suggest_soul_updates(
            observe=observe, soul_text="x", min_signals=0,
        )
        rows = memory._conn.execute(  # type: ignore[attr-defined]
            "SELECT key FROM facts WHERE key LIKE 'soul_update_suggestion.%'"
        ).fetchall()
        key = rows[0][0]
        initial = _confidence_for_key(memory, key)
        # Hit dedup many times — confidence must rise but stay ≤ 1.0.
        for _ in range(50):
            loop.suggest_soul_updates(
                observe=observe, soul_text="x", min_signals=0,
            )
        final = _confidence_for_key(memory, key)
        self.assertGreater(final, initial)
        self.assertLessEqual(final, 1.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
