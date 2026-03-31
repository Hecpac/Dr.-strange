from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Budget: max seconds a single proactive action may take.
DEFAULT_ACTION_BUDGET = 15.0

# Tick interval hint (actual scheduling is in CronScheduler).
DEFAULT_TICK_INTERVAL = 1800  # 30 minutes


@dataclass(slots=True)
class TickDecision:
    """What KAIROS decided after a tick."""

    action: str  # "none" or a short action verb
    reason: str = ""
    detail: str = ""
    duration_seconds: float = 0.0
    error: str = ""


@dataclass(slots=True)
class KairosState:
    """Lightweight running state for the proactive agent."""

    ticks: int = 0
    actions_taken: int = 0
    last_tick_at: float = 0.0
    last_action: str = ""


class KairosService:
    """Always-on proactive agent that evaluates context on each tick
    and decides whether to take an action without user input.

    Inspired by Claude Code's KAIROS feature. Receives periodic tick
    signals, gathers lightweight context (heartbeat, recent events,
    pending approvals), asks the LLM whether any proactive action is
    warranted, and executes it within a time budget.
    """

    def __init__(
        self,
        *,
        router: Any,
        heartbeat: Any,
        observe: Any,
        action_budget: float = DEFAULT_ACTION_BUDGET,
        brief: bool = True,
    ) -> None:
        self.router = router
        self.heartbeat = heartbeat
        self.observe = observe
        self.action_budget = action_budget
        self.brief = brief
        self.state = KairosState()

    def tick(self) -> TickDecision:
        """Called periodically by the CronScheduler. Gather context, decide, act."""
        start = time.time()
        self.state.ticks += 1
        self.state.last_tick_at = start

        try:
            context = self._gather_context()
            decision = self._decide(context)

            if decision.action == "none":
                self.observe.emit("kairos_tick", payload={
                    "action": "none",
                    "tick": self.state.ticks,
                })
                decision.duration_seconds = time.time() - start
                return decision

            # Execute within budget
            elapsed = time.time() - start
            remaining = self.action_budget - elapsed
            if remaining <= 0:
                decision.error = "budget_exhausted_before_action"
                self.observe.emit("kairos_tick", payload={
                    "action": decision.action,
                    "error": decision.error,
                    "tick": self.state.ticks,
                })
                decision.duration_seconds = time.time() - start
                return decision

            decision = self._execute(decision, budget=remaining)
            self.state.actions_taken += 1
            self.state.last_action = decision.action

            self.observe.emit("kairos_tick", payload={
                "action": decision.action,
                "reason": decision.reason,
                "duration": decision.duration_seconds,
                "tick": self.state.ticks,
            })
            return decision

        except Exception as exc:
            logger.exception("KAIROS tick failed")
            d = TickDecision(action="none", error=str(exc))
            d.duration_seconds = time.time() - start
            return d

    def _gather_context(self) -> str:
        """Build a brief context snapshot for the LLM."""
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

        parts.append(f"Ticks so far: {self.state.ticks}, actions taken: {self.state.actions_taken}")
        return "\n".join(parts)

    def _decide(self, context: str) -> TickDecision:
        """Ask the LLM whether to act, given current context."""
        brevity = " Be extremely brief." if self.brief else ""

        prompt = (
            "You are KAIROS, a proactive always-on agent. You receive periodic ticks "
            "and decide whether any action is needed based on current system state.\n\n"
            f"## Current State\n{context}\n\n"
            "## Rules\n"
            "- Only act if there is a clear, useful action to take right now.\n"
            "- Prefer 'none' over noisy or speculative actions.\n"
            "- Actions must complete in under 15 seconds.\n"
            f"{brevity}\n\n"
            "Respond with ONLY a JSON object:\n"
            '{"action": "none"} or {"action": "<verb>", "reason": "<why>", "detail": "<specifics>"}'
        )

        try:
            response = self.router.ask(
                prompt,
                lane="judge",
                evidence_pack={"kairos_context": context},
            )
            return self._parse_decision(response.content)
        except Exception as exc:
            logger.warning("KAIROS decide failed: %s", exc)
            return TickDecision(action="none", error=str(exc))

    @staticmethod
    def _parse_decision(text: str) -> TickDecision:
        """Parse LLM JSON response into a TickDecision."""
        try:
            clean = text.strip()
            start = clean.find("{")
            end = clean.rfind("}") + 1
            if start == -1 or end == 0:
                return TickDecision(action="none")
            data = json.loads(clean[start:end])
            return TickDecision(
                action=data.get("action", "none"),
                reason=data.get("reason", ""),
                detail=data.get("detail", ""),
            )
        except (json.JSONDecodeError, ValueError):
            return TickDecision(action="none")

    def _execute(self, decision: TickDecision, *, budget: float) -> TickDecision:
        """Execute the decided action within the remaining budget.

        Currently logs the decision. Subclasses or future versions
        can dispatch to tools (notifications, PR subscriptions, etc.).
        """
        start = time.time()
        # For now, KAIROS actions are advisory — logged and emitted.
        # Future: dispatch to actual tool handlers based on decision.action
        logger.info(
            "KAIROS action=%s reason=%s detail=%s",
            decision.action, decision.reason, decision.detail,
        )
        decision.duration_seconds = time.time() - start
        return decision
