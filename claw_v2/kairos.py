from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from claw_v2.tracing import attach_trace, child_trace_context, new_trace_context

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
        bus: Any | None = None,
        approvals: Any | None = None,
        sub_agents: Any | None = None,
        auto_research: Any | None = None,
        wiki: Any | None = None,
        action_budget: float = DEFAULT_ACTION_BUDGET,
        brief: bool = True,
    ) -> None:
        self.router = router
        self.heartbeat = heartbeat
        self.observe = observe
        self.bus = bus
        self.approvals = approvals
        self.sub_agents = sub_agents
        self.auto_research = auto_research
        self.wiki = wiki
        self.action_budget = action_budget
        self.brief = brief
        self.state = KairosState()

    def tick(self) -> TickDecision:
        """Called periodically by the CronScheduler. Gather context, decide, act."""
        start = time.time()
        trace = new_trace_context(artifact_id="kairos_tick")
        self.state.ticks += 1
        self.state.last_tick_at = start

        try:
            context = self._gather_context()
            decision = self._decide(context, trace)

            if decision.action == "none":
                self.observe.emit(
                    "kairos_tick",
                    trace_id=trace["trace_id"],
                    root_trace_id=trace["root_trace_id"],
                    span_id=trace["span_id"],
                    parent_span_id=trace["parent_span_id"],
                    artifact_id=trace["artifact_id"],
                    payload={
                        "action": "none",
                        "tick": self.state.ticks,
                    },
                )
                decision.duration_seconds = time.time() - start
                return decision

            # Execute within budget
            elapsed = time.time() - start
            remaining = self.action_budget - elapsed
            if remaining <= 0:
                decision.error = "budget_exhausted_before_action"
                self.observe.emit(
                    "kairos_tick",
                    trace_id=trace["trace_id"],
                    root_trace_id=trace["root_trace_id"],
                    span_id=trace["span_id"],
                    parent_span_id=trace["parent_span_id"],
                    artifact_id=trace["artifact_id"],
                    payload={
                        "action": decision.action,
                        "error": decision.error,
                        "tick": self.state.ticks,
                    },
                )
                decision.duration_seconds = time.time() - start
                return decision

            decision = self._execute(decision, budget=remaining, trace_context=trace)
            self.state.actions_taken += 1
            self.state.last_action = decision.action

            self.observe.emit(
                "kairos_tick",
                trace_id=trace["trace_id"],
                root_trace_id=trace["root_trace_id"],
                span_id=trace["span_id"],
                parent_span_id=trace["parent_span_id"],
                artifact_id=trace["artifact_id"],
                payload={
                    "action": decision.action,
                    "reason": decision.reason,
                    "duration": decision.duration_seconds,
                    "tick": self.state.ticks,
                },
            )
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

        # Bus: urgent messages and expired requests
        if self.bus is not None:
            try:
                urgent = self.bus.pending_urgent()
                if urgent:
                    parts.append(f"Urgent bus messages: {len(urgent)}")
                    for msg in urgent[:3]:
                        parts.append(f"  [{msg.from_agent}->{msg.to_agent}] {msg.topic}: {str(msg.payload)[:100]}")
                expired = self.bus.scan_expired_requests()
                if expired:
                    parts.append(f"Expired requests: {len(expired)}")
                    for msg in expired[:3]:
                        parts.append(f"  [{msg.from_agent}->{msg.to_agent}] {msg.topic}")
            except Exception:
                parts.append("Bus: unavailable")

        # Cost per agent
        try:
            costs = self.observe.cost_per_agent_today()
            if costs:
                cost_lines = [f"  {name}: ${cost:.2f}" for name, cost in costs.items()]
                parts.append("Cost today:\n" + "\n".join(cost_lines))
        except Exception:
            logger.debug("Failed to retrieve cost data", exc_info=True)

        parts.append(f"Ticks so far: {self.state.ticks}, actions taken: {self.state.actions_taken}")
        return "\n".join(parts)

    def _decide(self, context: str, trace_context: dict[str, Any] | None = None) -> TickDecision:
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
            "- Available actions: none, notify_user, dispatch_to_agent, approve_pending, "
            "run_skill, pause_agent, escalate_to_human, wiki_deep_lint.\n"
            "- wiki_deep_lint: run when wiki has accumulated changes since last audit.\n"
            f"{brevity}\n\n"
            "Respond with ONLY a JSON object:\n"
            '{"action": "none"} or {"action": "<verb>", "reason": "<why>", "detail": "<specifics>"}'
        )

        try:
            decision_trace = child_trace_context(trace_context, artifact_id="kairos_decide")
            response = self.router.ask(
                prompt,
                lane="judge",
                evidence_pack=attach_trace({"kairos_context": context}, decision_trace),
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

    def _execute(self, decision: TickDecision, *, budget: float, trace_context: dict[str, Any] | None = None) -> TickDecision:
        """Execute the decided action within the remaining budget."""
        start = time.time()
        handlers = {
            "notify_user": self._handle_notify_user,
            "dispatch_to_agent": self._handle_dispatch_to_agent,
            "approve_pending": self._handle_approve_pending,
            "run_skill": self._handle_run_skill,
            "pause_agent": self._handle_pause_agent,
            "escalate_to_human": self._handle_escalate_to_human,
            "wiki_deep_lint": self._handle_wiki_deep_lint,
        }
        handler = handlers.get(decision.action)
        if handler is None:
            decision.error = f"unknown action: {decision.action}"
            logger.warning("KAIROS unknown action: %s", decision.action)
        else:
            try:
                handler(decision, child_trace_context(trace_context, artifact_id=decision.action))
            except Exception as exc:
                decision.error = str(exc)
                logger.exception("KAIROS action %s failed", decision.action)
        decision.duration_seconds = time.time() - start
        return decision

    def _handle_notify_user(self, decision: TickDecision, trace_context: dict[str, Any] | None = None) -> None:
        logger.info("KAIROS notify_user: %s", decision.detail)
        self.observe.emit(
            "kairos_notify_user",
            trace_id=trace_context.get("trace_id") if trace_context else None,
            root_trace_id=trace_context.get("root_trace_id") if trace_context else None,
            span_id=trace_context.get("span_id") if trace_context else None,
            parent_span_id=trace_context.get("parent_span_id") if trace_context else None,
            job_id=trace_context.get("job_id") if trace_context else None,
            artifact_id=trace_context.get("artifact_id") if trace_context else None,
            payload={"message": decision.detail},
        )

    def _handle_dispatch_to_agent(self, decision: TickDecision, trace_context: dict[str, Any] | None = None) -> None:
        if self.bus is None:
            raise RuntimeError("Bus not configured")
        from claw_v2.bus import _new_message
        data = json.loads(decision.detail)
        msg = _new_message(
            from_agent="kairos",
            to_agent=data["to_agent"],
            intent="notify",
            topic=data["topic"],
            payload=data.get("payload", {}),
            priority="normal",
        )
        self.bus.send(msg)

    def _handle_approve_pending(self, decision: TickDecision, trace_context: dict[str, Any] | None = None) -> None:
        if self.approvals is None:
            raise RuntimeError("Approvals not configured")
        data = json.loads(decision.detail)
        approval_id = data["approval_id"]
        approved = self.approvals.approve_internal(approval_id)
        if not approved:
            raise RuntimeError(f"approval could not be auto-approved: {approval_id}")
        self.observe.emit(
            "kairos_auto_approved",
            trace_id=trace_context.get("trace_id") if trace_context else None,
            root_trace_id=trace_context.get("root_trace_id") if trace_context else None,
            span_id=trace_context.get("span_id") if trace_context else None,
            parent_span_id=trace_context.get("parent_span_id") if trace_context else None,
            job_id=trace_context.get("job_id") if trace_context else None,
            artifact_id=trace_context.get("artifact_id") if trace_context else None,
            payload={"approval_id": approval_id},
        )

    def _handle_run_skill(self, decision: TickDecision, trace_context: dict[str, Any] | None = None) -> None:
        if self.sub_agents is None:
            raise RuntimeError("Sub-agent service not configured")
        data = json.loads(decision.detail)
        lane = data.get("lane", "worker")
        result = self.sub_agents.run_skill(data["agent"], data["skill"], data.get("context", ""), lane=lane)
        logger.info("KAIROS run_skill: agent=%s skill=%s", data["agent"], data["skill"])
        self.observe.emit(
            "kairos_run_skill",
            trace_id=trace_context.get("trace_id") if trace_context else None,
            root_trace_id=trace_context.get("root_trace_id") if trace_context else None,
            span_id=trace_context.get("span_id") if trace_context else None,
            parent_span_id=trace_context.get("parent_span_id") if trace_context else None,
            job_id=trace_context.get("job_id") if trace_context else None,
            artifact_id=trace_context.get("artifact_id") if trace_context else None,
            payload={**data, "lane": lane, "result": result},
        )

    def _handle_pause_agent(self, decision: TickDecision, trace_context: dict[str, Any] | None = None) -> None:
        data = json.loads(decision.detail)
        agent_name = data["agent_name"]
        reason = data["reason"]
        if self.auto_research is not None:
            try:
                self.auto_research.pause(agent_name)
            except FileNotFoundError:
                if self.sub_agents is None or self.sub_agents.get_agent(agent_name) is None:
                    raise
        self.observe.emit(
            "agent_paused",
            trace_id=trace_context.get("trace_id") if trace_context else None,
            root_trace_id=trace_context.get("root_trace_id") if trace_context else None,
            span_id=trace_context.get("span_id") if trace_context else None,
            parent_span_id=trace_context.get("parent_span_id") if trace_context else None,
            job_id=trace_context.get("job_id") if trace_context else None,
            artifact_id=trace_context.get("artifact_id") if trace_context else None,
            payload={"agent_name": agent_name, "reason": reason},
        )
        logger.info("KAIROS paused agent %s: %s", agent_name, reason)

    def _handle_escalate_to_human(self, decision: TickDecision, trace_context: dict[str, Any] | None = None) -> None:
        logger.info("KAIROS escalate: %s", decision.detail)
        self.observe.emit(
            "kairos_escalate",
            trace_id=trace_context.get("trace_id") if trace_context else None,
            root_trace_id=trace_context.get("root_trace_id") if trace_context else None,
            span_id=trace_context.get("span_id") if trace_context else None,
            parent_span_id=trace_context.get("parent_span_id") if trace_context else None,
            job_id=trace_context.get("job_id") if trace_context else None,
            artifact_id=trace_context.get("artifact_id") if trace_context else None,
            payload={"message": decision.detail},
        )

    def _handle_wiki_deep_lint(self, decision: TickDecision, trace_context: dict[str, Any] | None = None) -> None:
        if self.wiki is None:
            raise RuntimeError("WikiService not configured")
        result = self.wiki.deep_lint()
        logger.info(
            "KAIROS wiki_deep_lint: issues=%d contradictions=%d stale=%d gaps=%d",
            result.get("issues", 0),
            len(result.get("contradictions", [])),
            len(result.get("stale", [])),
            len(result.get("gaps", [])),
        )
        self.observe.emit(
            "kairos_wiki_deep_lint",
            trace_id=trace_context.get("trace_id") if trace_context else None,
            root_trace_id=trace_context.get("root_trace_id") if trace_context else None,
            span_id=trace_context.get("span_id") if trace_context else None,
            parent_span_id=trace_context.get("parent_span_id") if trace_context else None,
            job_id=trace_context.get("job_id") if trace_context else None,
            artifact_id=trace_context.get("artifact_id") if trace_context else None,
            payload=result,
        )
