"""Learning loop — records outcomes, retrieves lessons, derives insights via LLM."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claw_v2.llm import LLMRouter
    from claw_v2.memory import MemoryStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LearningLoop:
    memory: MemoryStore
    router: LLMRouter | None = None
    _last_outcome_id: int | None = field(default=None, repr=False)

    # --- Record ---

    def record(
        self,
        *,
        task_type: str,
        task_id: str,
        description: str,
        approach: str,
        outcome: str,
        error_snippet: str | None = None,
        retries: int = 0,
        lesson: str | None = None,
    ) -> int:
        """Record a task outcome. Derives lesson via LLM if not provided."""
        if not lesson:
            lesson = self._derive_lesson(description, approach, outcome, error_snippet)
        oid = self.memory.store_task_outcome(
            task_type=task_type,
            task_id=task_id,
            description=description,
            approach=approach,
            outcome=outcome,
            lesson=lesson,
            error_snippet=error_snippet,
            retries=retries,
        )
        self._last_outcome_id = oid
        logger.info("Learning loop recorded outcome #%d (%s/%s)", oid, task_type, outcome)
        return oid

    # --- Retrieve ---

    def retrieve_lessons(self, context: str, *, task_type: str | None = None, limit: int = 3) -> str:
        """Retrieve relevant past lessons formatted for injection into a prompt."""
        # Strip common preamble markers to extract the actual user message.
        clean = context
        for marker in ("# Current input\n", "# Profile facts\n", "# Recent messages\n", "# Learning rules\n"):
            if marker in clean:
                clean = clean.split(marker)[-1]
        # Use the last meaningful line (most likely the user's actual query) if multi-line.
        lines = [ln.strip() for ln in clean.strip().splitlines() if ln.strip() and not ln.startswith("#")]
        keywords = " ".join(lines[-1].split()[:20]) if lines else " ".join(context.split()[:20])
        outcomes = self.memory.search_past_outcomes(keywords, task_type=task_type, limit=limit)
        if not outcomes:
            seen_task_ids: set[str] = set()
            token_matches: list[dict] = []
            tokens = [token for token in keywords.split() if len(token) >= 4]
            for token in tokens:
                for match in self.memory.search_past_outcomes(token, task_type=task_type, limit=limit):
                    task_id = match.get("task_id")
                    if task_id in seen_task_ids:
                        continue
                    seen_task_ids.add(task_id)
                    token_matches.append(match)
                    if len(token_matches) >= limit:
                        break
                if len(token_matches) >= limit:
                    break
            outcomes = token_matches
        if not outcomes:
            outcomes = self.memory.recent_failures(task_type=task_type, limit=limit)
        if not outcomes:
            return ""
        lines: list[str] = ["# Lessons from past tasks"]
        for o in outcomes:
            status = "OK" if o["outcome"] == "success" else "FAIL"
            fb = ""
            if o.get("feedback"):
                fb = f" | User feedback: {o['feedback']}"
            lines.append(f"- [{status}] {o['description'][:80]}")
            lines.append(f"  Lesson: {o['lesson']}{fb}")
            if o.get("error_snippet"):
                lines.append(f"  Error: {o['error_snippet'][:200]}")
        return "\n".join(lines)

    # --- Feedback ---

    def feedback(self, outcome_id: int | None, rating: str) -> str:
        """Attach user feedback (positive/negative/note) to the most recent or specified outcome."""
        oid = outcome_id or self._last_outcome_id or self.memory.last_outcome_id()
        if not oid:
            return "No outcomes recorded yet."
        existing = self.memory.get_outcome(oid)
        if not existing:
            return f"Outcome #{oid} not found."
        self.memory.update_outcome_feedback(oid, rating)
        return f"Feedback '{rating}' saved for outcome #{oid}: {existing['description'][:60]}"

    # --- Derive lesson ---

    def _derive_lesson(
        self, description: str, approach: str, outcome: str, error_snippet: str | None,
    ) -> str:
        """Use LLM to derive a lesson from the outcome. Falls back to heuristics."""
        if self.router:
            try:
                return self._derive_lesson_llm(description, approach, outcome, error_snippet)
            except Exception:
                logger.warning("LLM lesson derivation failed, falling back to heuristics")
        return self._derive_lesson_heuristic(outcome, error_snippet)

    def _derive_lesson_llm(
        self, description: str, approach: str, outcome: str, error_snippet: str | None,
    ) -> str:
        prompt = (
            f"A task just completed. Extract ONE concise lesson (max 2 sentences) "
            f"that would help a future AI agent avoid the same mistake or replicate the success.\n\n"
            f"Task: {description[:300]}\n"
            f"Approach: {approach[:200]}\n"
            f"Outcome: {outcome}\n"
        )
        if error_snippet:
            prompt += f"Error: {error_snippet[:500]}\n"
        prompt += "\nLesson:"
        resp = self.router.ask(prompt, lane="judge", max_budget=0.05, timeout=30.0)  # type: ignore[union-attr]
        return resp.content.strip()[:500]

    @staticmethod
    def _derive_lesson_heuristic(outcome: str, error_snippet: str | None) -> str:
        if outcome == "success":
            return "Task completed successfully."
        snippet = (error_snippet or "").lower()
        if "import" in snippet and "error" in snippet:
            return "Import errors — check module paths and dependencies."
        if "assert" in snippet:
            return "Assertion failures — verify expected values match implementation."
        if "timeout" in snippet:
            return "Test timeouts — check for infinite loops or slow operations."
        if "permission" in snippet:
            return "Permission errors — check file/directory access rights."
        return "Task failed. Review error output for root cause."

    # --- Consolidation ---

    def consolidate(self, *, min_outcomes: int = 10) -> str | None:
        """Aggregate recent outcomes into consolidated lessons. Runs periodically."""
        if not self.router:
            return None
        outcomes = self.memory.search_past_outcomes("", limit=min_outcomes)
        if len(outcomes) < min_outcomes:
            return None
        summary_lines: list[str] = []
        for o in outcomes:
            fb = f" (feedback: {o.get('feedback', 'none')})"
            summary_lines.append(f"- [{o['outcome']}] {o['description'][:80]} → {o['lesson']}{fb}")
        prompt = (
            "Review these AI agent task outcomes and extract 3-5 actionable rules "
            "that should guide future behavior. Focus on patterns, not individual cases.\n\n"
            + "\n".join(summary_lines)
            + "\n\nRules:"
        )
        try:
            resp = self.router.ask(prompt, lane="judge", max_budget=0.10, timeout=60.0)
            rules = resp.content.strip()
            self.memory.store_fact(
                key="learning_loop_consolidated",
                value=rules,
                source="learning_loop",
                source_trust="self",
                confidence=0.7,
                entity_tags='["learning", "consolidated"]',
            )
            logger.info("Learning loop consolidated %d outcomes into rules", len(outcomes))
            return rules
        except Exception:
            logger.warning("Learning loop consolidation failed")
            return None
