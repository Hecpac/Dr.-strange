from __future__ import annotations

import fcntl
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DREAM_LOCK_PATH = Path.home() / ".claw" / "dream.lock"


@dataclass(slots=True)
class DreamResult:
    pruned: int
    consolidated: int
    duration_seconds: float
    skipped: bool = False
    reason: str = ""


class AutoDreamService:
    """Memory consolidation service inspired by Claude Code's autoDream.

    Runs periodically to orient, gather signal, consolidate, and prune
    the memory store — keeping it fresh and contradiction-free.
    """

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

    def tick_session(self) -> None:
        """Call once per session to track session count."""
        self._sessions_since_dream += 1

    def should_dream(self) -> tuple[bool, str]:
        """Check if dreaming conditions are met."""
        hours_elapsed = (time.time() - self._last_dream_at) / 3600
        if hours_elapsed >= self.min_hours_between_dreams:
            return True, "time_elapsed"
        if self._sessions_since_dream >= self.min_sessions_between_dreams:
            return True, "session_count"
        return False, ""

    def run(self) -> DreamResult:
        """Execute the full dream cycle: import → orient → gather → consolidate → export → prune."""
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
            self._export_shared(existing_facts[:20])
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

    def _export_shared(self, facts: list[dict]) -> int:
        """Post-consolidation: write high-confidence facts to shared-memory exports."""
        import json as _json
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
                    f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
                    count += 1
        return count

    def _import_shared(self) -> list[dict]:
        """Pre-orient: read exports from other agents, filtered by tags."""
        import json as _json
        if not self.shared_memory_root.exists():
            return []
        imported: list[dict] = []
        existing_keys = {f.get("key") for f in self.memory.search_facts("", limit=self.max_facts * 2, agent_name=self.agent_name)}
        for export_file in self.shared_memory_root.glob("*_exports.jsonl"):
            if export_file.stem.replace("_exports", "") == self.agent_name:
                continue
            for line in export_file.read_text(encoding="utf-8").strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    entry = _json.loads(line)
                except (ValueError, _json.JSONDecodeError):
                    continue
                tags = set(entry.get("tags", []))
                if "personal" in tags and self.agent_name != "alma":
                    continue
                if self.import_tags and not tags.intersection(self.import_tags):
                    continue
                if entry.get("key") in existing_keys:
                    continue
                imported.append(entry)
        return imported

    def _orient(self) -> list[dict]:
        """Phase 1: Read existing memory, list all facts."""
        facts = self.memory.search_facts("", limit=self.max_facts * 2)
        logger.info("autoDream orient: %d existing facts", len(facts))
        return facts

    def _gather_signal(self) -> list[dict]:
        """Phase 2: Extract new information from recent events."""
        events = self.observe.recent_events(limit=100)
        signals = []
        for event in events:
            if event.get("event_type") in ("llm_response", "morning_brief", "sub_agents_discovered", "self_improve_agent_done"):
                signals.append(event)
        logger.info("autoDream gather: %d signals from recent events", len(signals))
        return signals

    def _consolidate(self, existing_facts: list[dict], new_signals: list[dict]) -> int:
        """Phase 3: Use LLM to merge duplicates, resolve contradictions, update dates."""
        if not existing_facts:
            return 0

        facts_text = "\n".join(
            f"- [{f.get('key', '?')}] {f.get('value', '')[:200]} (confidence={f.get('confidence', 0)}, source={f.get('source', '?')})"
            for f in existing_facts[:50]
        )
        signals_text = "\n".join(
            f"- [{s.get('event_type', '?')}] {str(s.get('payload', ''))[:200]}"
            for s in new_signals[:20]
        ) if new_signals else "(no new signals)"

        evidence = (
            f"EXISTING FACTS:\n{facts_text}\n\n"
            f"RECENT SIGNALS:\n{signals_text}"
        )

        prompt = (
            "You are a memory consolidation agent. Review the supplied evidence and output a JSON array of actions.\n\n"
            "Rules:\n"
            '1. Find duplicate or contradictory facts → output {"action": "delete", "key": "<key>"}\n'
            '2. Find facts with relative dates → output {"action": "update", "key": "<key>", "value": "<new value with absolute date>"}\n'
            '3. Find important new info from signals → output {"action": "create", "key": "<key>", "value": "<value>", "source": "dream"}\n'
            "4. If nothing to do, return empty array: []\n\n"
            "Output ONLY a JSON array, no explanation."
        )

        try:
            response = self.router.ask(
                prompt,
                lane=self.lane,
                evidence_pack={"context": evidence},
            )
            return self._apply_actions(response.content)
        except Exception:
            logger.exception("autoDream consolidate LLM call failed")
            return 0

    def _apply_actions(self, llm_response: str) -> int:
        """Parse LLM response and apply memory actions with verification."""
        import json

        try:
            text = llm_response.strip()
            start = text.find("[")
            end = text.rfind("]") + 1
            if start == -1 or end == 0:
                return 0
            actions = json.loads(text[start:end])
        except (json.JSONDecodeError, ValueError):
            logger.warning("autoDream: could not parse LLM actions")
            return 0

        count = 0
        for action in actions:
            act = action.get("action")
            key = action.get("key", "")
            if not key:
                continue
            if act == "delete":
                # Verify: only delete if the fact actually exists and has low confidence.
                existing = self.memory.search_facts(key, limit=1, agent_name=self.agent_name)
                if not existing:
                    existing = self.memory.search_facts(key, limit=1)
                if existing and existing[0].get("confidence", 1.0) < 0.6:
                    self.memory.delete_fact(key)
                    count += 1
                else:
                    logger.info("autoDream: skipped delete of '%s' (not found or confidence >= 0.6)", key)
            elif act == "update" and action.get("value"):
                # Verify: only update if original fact exists.
                existing = self.memory.search_facts(key, limit=1)
                if existing:
                    self.memory.store_fact(key, action["value"], source="dream", confidence=0.7)
                    count += 1
                else:
                    logger.info("autoDream: skipped update of '%s' (original not found)", key)
            elif act == "create":
                # Verify: don't create duplicates.
                existing = self.memory.search_facts(key, limit=1)
                if not existing or existing[0].get("key") != key:
                    self.memory.store_fact(key, action.get("value", ""), source=action.get("source", "dream"), confidence=0.5)
                    count += 1
                else:
                    logger.info("autoDream: skipped create of '%s' (already exists)", key)
        return count

    def _prune(self) -> int:
        """Phase 4: Remove low-confidence and stale facts, keep under max_facts."""
        all_facts = self.memory.search_facts("", limit=self.max_facts * 2)
        if len(all_facts) <= self.max_facts:
            return 0

        sorted_facts = sorted(all_facts, key=lambda f: f.get("confidence", 0))
        to_prune = len(all_facts) - self.max_facts
        pruned = 0
        for fact in sorted_facts:
            if pruned >= to_prune:
                break
            key = fact.get("key", "")
            if key and not key.startswith("profile."):
                self.memory.delete_fact(key)
                pruned += 1
        logger.info("autoDream prune: removed %d facts (had %d, max %d)", pruned, len(all_facts), self.max_facts)
        return pruned


def _acquire_lock() -> bool:
    """Try to acquire the dream lock file."""
    DREAM_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        f = DREAM_LOCK_PATH.open("w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.write(str(time.time()))
        f.flush()
        _acquire_lock._file = f  # type: ignore[attr-defined]
        return True
    except (BlockingIOError, OSError):
        return False


def _release_lock() -> None:
    """Release the dream lock file."""
    f = getattr(_acquire_lock, "_file", None)
    if f is not None:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
            DREAM_LOCK_PATH.unlink(missing_ok=True)
        finally:
            f.close()
            _acquire_lock._file = None  # type: ignore[attr-defined]
