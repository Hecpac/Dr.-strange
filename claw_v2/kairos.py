from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from claw_v2.a2a import A2AService
    from claw_v2.skills import SkillRegistry

from claw_v2.approval_gate import system_approval_mode
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
        task_board: Any | None = None,
        monitored_sites: list[Any] | None = None,
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
        self.task_board = task_board
        self.skill_registry: SkillRegistry | None = None
        self.a2a: A2AService | None = None
        self.nlm_service: Any | None = None
        self.monitored_sites = list(monitored_sites or [])
        self.action_budget = action_budget
        self.brief = brief
        self.state = KairosState()

    def tick(self) -> TickDecision:
        """Called periodically by the CronScheduler. Gather context, decide, act."""
        start = time.time()
        trace = new_trace_context(artifact_id="kairos_tick")
        self.state.ticks += 1
        self.state.last_tick_at = start

        # Paso 3 Last-Mile (HEC-14): any Tier 3 tool call triggered during this
        # tick is an autonomous scheduler action, not an interactive one.
        # Switch the shared tool executor to auto-approve-with-audit mode so
        # the daemon doesn't deadlock on ApprovalPending while leaving a trail.
        with system_approval_mode(reason="Scheduled Kairos Tick"):
            return self._tick_body(start, trace)

    def _tick_body(self, start: float, trace: dict) -> TickDecision:
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

    def handle_event(self, event_type: str, payload: dict[str, Any] | None = None) -> TickDecision:
        start = time.time()
        trace = new_trace_context(artifact_id=f"kairos_event:{event_type}")
        event_payload = payload or {}
        context = "\n".join(
            [
                f"Event trigger: {event_type}",
                f"Event payload: {json.dumps(event_payload, ensure_ascii=True, sort_keys=True)[:1000]}",
                self._gather_context(),
            ]
        )
        decision = self._decide(context, trace)
        if decision.action != "none":
            remaining = max(self.action_budget - (time.time() - start), 0.0)
            decision = self._execute(decision, budget=remaining, trace_context=trace)
        decision.duration_seconds = time.time() - start
        self.observe.emit(
            "kairos_event",
            trace_id=trace["trace_id"],
            root_trace_id=trace["root_trace_id"],
            span_id=trace["span_id"],
            parent_span_id=trace["parent_span_id"],
            artifact_id=trace["artifact_id"],
            payload={"event_type": event_type, "action": decision.action, "reason": decision.reason, "error": decision.error},
        )
        return decision

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

        # Task board status
        if self.task_board is not None:
            try:
                summary = self.task_board.summary()
                if summary:
                    parts.append(f"Task board: {summary}")
                pending = self.task_board.pending()
                if pending:
                    task_lines = [f"  [{t.id}] {t.title} (lane={t.required_lane}, by={t.created_by})" for t in pending[:5]]
                    parts.append("Pending tasks:\n" + "\n".join(task_lines))
            except Exception:
                parts.append("Task board: unavailable")

        parts.append(f"Ticks so far: {self.state.ticks}, actions taken: {self.state.actions_taken}")
        return "\n".join(parts)

    def _decide(self, context: str, trace_context: dict[str, Any] | None = None) -> TickDecision:
        """Ask the LLM whether to act, given current context."""
        brevity = " Be extremely brief." if self.brief else ""
        site_names = ", ".join(name for name, _ in self._monitored_site_pairs()) or "configured monitored sites"

        prompt = (
            "You are KAIROS, a proactive always-on agent. You receive periodic ticks "
            "and decide whether any action is needed based on current system state.\n\n"
            f"## Current State\n{context}\n\n"
            "## Rules\n"
            "- Only act if there is a clear, useful action to take right now.\n"
            "- Prefer 'none' over noisy or speculative actions.\n"
            "- Actions must complete in under 15 seconds.\n"
            "- Available actions: none, notify_user, dispatch_to_agent, approve_pending, "
            "run_skill, pause_agent, escalate_to_human, wiki_deep_lint, wiki_research, "
            "wiki_scrape, site_monitor, auto_publish_social, auto_deploy, gmail_digest, publish_task, claim_task.\n"
            "- publish_task: add a task to the shared board for any agent to claim. "
            "detail = JSON {\"title\": \"...\", \"instruction\": \"...\", \"required_lane\": \"worker\", \"tags\": [...]}\n"
            "- claim_task: claim and execute a pending task from the board. "
            "detail = JSON {\"agent\": \"...\", \"lane\": \"worker\"}\n"
            "- wiki_deep_lint: run when wiki has accumulated changes since last audit.\n"
            "- wiki_research: proactively identify knowledge gaps and generate new wiki pages.\n"
            "- wiki_scrape: scrape watched sources for new content to ingest into wiki.\n"
            f"- site_monitor: check uptime of {site_names}.\n"
            "- auto_publish_social: draft and publish a tweet from wiki insights (Tier 3 — logs for review).\n"
            "- auto_deploy: trigger Vercel deploy if pending changes detected (Tier 3 — logs for review).\n"
            "- gmail_digest: summarize recent inbox and notify user of priorities.\n"
            "- generate_skill: create a new skill for a detected gap (Memento-Skills pattern). "
            "detail = task description for the skill to generate.\n"
            "- nlm_wiki_sync: extract knowledge from NotebookLM notebooks and ingest into wiki.\n"
            "- morning_video_brief: generate daily workout/briefing video via HeyGen and notify user.\n"
            "- a2a_send: send a task to a registered A2A peer agent. "
            "detail = JSON {\"to_agent\": \"...\", \"action\": \"...\", \"payload\": {...}}\n"
            f"{brevity}\n\n"
            "## Examples\n"
            "Context: Pending approvals: 0; Recent events: [heartbeat] all services healthy; Ticks so far: 3, actions taken: 0\n"
            'Decision: {"action": "none"}\n'
            "Context: Pending approvals: 1; Approval IDs: prod-deploy-123; Recent events: [critical_action_verification] risk=critical\n"
            'Decision: {"action": "notify_user", "reason": "critical approval pending", "detail": "Approval prod-deploy-123 requires review."}\n'
            "Context: Urgent bus messages: 1; [planner->hex] test_failure: retry failing pytest target\n"
            'Decision: {"action": "dispatch_to_agent", "reason": "urgent test failure needs an operator", "detail": "{\\"to_agent\\": \\"hex\\", \\"topic\\": \\"test_failure\\", \\"payload\\": {\\"instruction\\": \\"retry failing pytest target\\"}}"}\n'
            "Context: Recent events: [observe] routine event; Task board: no pending tasks\n"
            'Decision: {"action": "none"}\n\n'
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
            "wiki_research": self._handle_wiki_research,
            "wiki_scrape": self._handle_wiki_scrape,
            "site_monitor": self._handle_site_monitor,
            "auto_publish_social": self._handle_auto_publish_social,
            "auto_deploy": self._handle_auto_deploy,
            "gmail_digest": self._handle_gmail_digest,
            "generate_skill": self._handle_generate_skill,
            "nlm_wiki_sync": self._handle_nlm_wiki_sync,
            "a2a_send": self._handle_a2a_send,
            "publish_task": self._handle_publish_task,
            "claim_task": self._handle_claim_task,
            "morning_video_brief": self._handle_morning_video_brief,
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
        if not self._notification_is_important(decision):
            logger.info("KAIROS suppressed noisy notification: %s", decision.detail)
            self.observe.emit(
                "kairos_notify_suppressed",
                trace_id=trace_context.get("trace_id") if trace_context else None,
                root_trace_id=trace_context.get("root_trace_id") if trace_context else None,
                span_id=trace_context.get("span_id") if trace_context else None,
                parent_span_id=trace_context.get("parent_span_id") if trace_context else None,
                job_id=trace_context.get("job_id") if trace_context else None,
                artifact_id=trace_context.get("artifact_id") if trace_context else None,
                payload={"message": decision.detail, "reason": decision.reason},
            )
            return
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

    def _notification_is_important(self, decision: TickDecision) -> bool:
        text = f"{decision.reason}\n{decision.detail}".strip()
        if not text:
            return False
        lowered = text.lower()
        if any(token in lowered for token in ("approval", "critical", "down", "failed", "blocked", "urgent", "security")):
            return True
        prompt = (
            "Decide whether this proactive notification should interrupt Hector.\n"
            "Return JSON only: {\"important\": true|false, \"reason\": \"...\"}.\n"
            "Important means the information changes a decision, requires action, or prevents risk. "
            "Noise should be logged but not sent.\n\n"
            f"Notification:\n{text[:1000]}"
        )
        try:
            response = self.router.ask(prompt, lane="judge", evidence_pack={"kairos_notification": text})
            raw = response.content.strip()
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start == -1 or end == 0:
                return True
            parsed = json.loads(raw[start:end])
            return bool(parsed.get("important", True))
        except Exception:
            logger.debug("KAIROS notification importance check failed", exc_info=True)
            return True

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

    def _handle_publish_task(self, decision: TickDecision, trace_context: dict[str, Any] | None = None) -> None:
        if self.task_board is None:
            raise RuntimeError("TaskBoard not configured")
        data = json.loads(decision.detail)
        task = self.task_board.publish(
            title=data["title"],
            instruction=data.get("instruction", ""),
            created_by="kairos",
            priority=data.get("priority", 0),
            required_lane=data.get("required_lane", "worker"),
            tags=data.get("tags", []),
        )
        self.observe.emit(
            "kairos_publish_task",
            trace_id=trace_context.get("trace_id") if trace_context else None,
            root_trace_id=trace_context.get("root_trace_id") if trace_context else None,
            span_id=trace_context.get("span_id") if trace_context else None,
            parent_span_id=trace_context.get("parent_span_id") if trace_context else None,
            job_id=trace_context.get("job_id") if trace_context else None,
            artifact_id=trace_context.get("artifact_id") if trace_context else None,
            payload={"task_id": task.id, "title": task.title},
        )

    def _handle_claim_task(self, decision: TickDecision, trace_context: dict[str, Any] | None = None) -> None:
        if self.task_board is None:
            raise RuntimeError("TaskBoard not configured")
        if self.sub_agents is None:
            raise RuntimeError("SubAgentService not configured")
        data = json.loads(decision.detail)
        agent_name = data.get("agent", "alma")
        lane = data.get("lane", "worker")
        task = self.task_board.claim(agent_name, lane=lane)
        if task is None:
            decision.error = "no pending tasks matching criteria"
            return
        self.task_board.start(task.id)
        try:
            result = self.sub_agents.dispatch(agent_name, task.instruction, lane=lane)
            self.task_board.complete(task.id, result)
        except Exception as exc:
            self.task_board.fail(task.id, str(exc))
            raise
        self.observe.emit(
            "kairos_claim_task",
            trace_id=trace_context.get("trace_id") if trace_context else None,
            root_trace_id=trace_context.get("root_trace_id") if trace_context else None,
            span_id=trace_context.get("span_id") if trace_context else None,
            parent_span_id=trace_context.get("parent_span_id") if trace_context else None,
            job_id=trace_context.get("job_id") if trace_context else None,
            artifact_id=trace_context.get("artifact_id") if trace_context else None,
            payload={"task_id": task.id, "agent": agent_name, "title": task.title},
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

    def _handle_wiki_research(self, decision: TickDecision, trace_context: dict[str, Any] | None = None) -> None:
        if self.wiki is None:
            raise RuntimeError("WikiService not configured")
        result = self.wiki.auto_research()
        logger.info(
            "KAIROS wiki_research: topics=%d pages_written=%d",
            result.get("topics_researched", 0),
            result.get("pages_written", 0),
        )
        self.observe.emit(
            "kairos_wiki_research",
            trace_id=trace_context.get("trace_id") if trace_context else None,
            root_trace_id=trace_context.get("root_trace_id") if trace_context else None,
            span_id=trace_context.get("span_id") if trace_context else None,
            parent_span_id=trace_context.get("parent_span_id") if trace_context else None,
            job_id=trace_context.get("job_id") if trace_context else None,
            artifact_id=trace_context.get("artifact_id") if trace_context else None,
            payload=result,
        )

    def _handle_wiki_scrape(self, decision: TickDecision, trace_context: dict[str, Any] | None = None) -> None:
        if self.wiki is None:
            raise RuntimeError("WikiService not configured")
        result = self.wiki.auto_scrape_sources()
        logger.info("KAIROS wiki_scrape: scraped=%d ingested=%d",
                     result.get("sources_scraped", 0), result.get("pages_ingested", 0))
        self.observe.emit("kairos_wiki_scrape",
                          trace_id=trace_context.get("trace_id") if trace_context else None,
                          root_trace_id=trace_context.get("root_trace_id") if trace_context else None,
                          span_id=trace_context.get("span_id") if trace_context else None,
                          parent_span_id=trace_context.get("parent_span_id") if trace_context else None,
                          job_id=trace_context.get("job_id") if trace_context else None,
                          artifact_id=trace_context.get("artifact_id") if trace_context else None,
                          payload=result)

    def _handle_site_monitor(self, decision: TickDecision, trace_context: dict[str, Any] | None = None) -> None:
        import httpx
        sites = dict(self._monitored_site_pairs())
        results = {}
        for name, url in sites.items():
            try:
                r = httpx.get(url, timeout=15, follow_redirects=True)
                results[name] = {"status": r.status_code, "ok": r.status_code < 400}
            except Exception as e:
                results[name] = {"status": 0, "ok": False, "error": str(e)}
        down = [n for n, r in results.items() if not r["ok"]]
        if down:
            self.observe.emit("site_down", payload={"sites": down, "details": results})
        logger.info("KAIROS site_monitor: %s", "ALL OK" if not down else f"DOWN: {down}")

    def _monitored_site_pairs(self) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for site in self.monitored_sites:
            name = str(getattr(site, "name", "")).strip()
            url = str(getattr(site, "url", "")).strip()
            if name and url:
                pairs.append((name, url))
        if pairs:
            return pairs
        return [
            ("premiumhome.design", "https://premiumhome.design"),
            ("pachanodesign.com", "https://www.pachanodesign.com"),
        ]

    def _handle_auto_publish_social(self, decision: TickDecision, trace_context: dict[str, Any] | None = None) -> None:
        """Draft a tweet from wiki insights and publish via X API."""
        if self.wiki is None:
            raise RuntimeError("WikiService not configured")
        pages = sorted(self.wiki.wiki_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not pages:
            decision.error = "No wiki pages to generate social content from"
            return
        page_content = pages[0].read_text(encoding="utf-8")[:2000]
        prompt = (
            "Draft a concise, professional tweet (max 280 chars) based on this knowledge article. "
            "Write in English. Include 1-2 relevant hashtags. No emojis.\n\n"
            f"Article:\n{page_content}"
        )
        resp = self.router.ask(prompt, lane="worker", max_budget=0.10, timeout=30.0)
        tweet_text = resp.content.strip().strip('"')
        if len(tweet_text) > 280:
            tweet_text = tweet_text[:277] + "..."
        from claw_v2.social import x_adapter_from_keychain
        adapter = x_adapter_from_keychain(handle="PachanoDesign")
        result = adapter.publish(tweet_text)
        logger.info("KAIROS auto_publish_social: published=%s id=%s", result.success, result.post_id)
        self.observe.emit("kairos_auto_publish_social",
                          trace_id=trace_context.get("trace_id") if trace_context else None,
                          root_trace_id=trace_context.get("root_trace_id") if trace_context else None,
                          span_id=trace_context.get("span_id") if trace_context else None,
                          parent_span_id=trace_context.get("parent_span_id") if trace_context else None,
                          job_id=trace_context.get("job_id") if trace_context else None,
                          artifact_id=trace_context.get("artifact_id") if trace_context else None,
                          payload={"tweet": tweet_text, "success": result.success, "post_id": result.post_id})

    def _handle_auto_deploy(self, decision: TickDecision, trace_context: dict[str, Any] | None = None) -> None:
        """Trigger Vercel deploy via git push if local is ahead."""
        import subprocess
        from pathlib import Path
        result = subprocess.run(
            ["gh", "api", "repos/Hecpac/PHD-Web/deployments", "--jq", ".[0].sha"],
            capture_output=True, text=True, timeout=15,
        )
        latest_deploy_sha = result.stdout.strip() if result.returncode == 0 else ""
        result2 = subprocess.run(
            ["git", "-C", str(Path.home() / "Projects" / "phd"), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        local_head = result2.stdout.strip() if result2.returncode == 0 else ""
        if not latest_deploy_sha or not local_head:
            decision.error = "Could not compare deploy state"
            return
        if latest_deploy_sha == local_head:
            logger.info("KAIROS auto_deploy: already up to date")
            return
        push = subprocess.run(
            ["git", "-C", str(Path.home() / "Projects" / "phd"), "push", "origin", "main"],
            capture_output=True, text=True, timeout=30,
        )
        success = push.returncode == 0
        logger.info("KAIROS auto_deploy: pushed=%s", success)
        self.observe.emit("kairos_auto_deploy",
                          trace_id=trace_context.get("trace_id") if trace_context else None,
                          root_trace_id=trace_context.get("root_trace_id") if trace_context else None,
                          span_id=trace_context.get("span_id") if trace_context else None,
                          parent_span_id=trace_context.get("parent_span_id") if trace_context else None,
                          job_id=trace_context.get("job_id") if trace_context else None,
                          artifact_id=trace_context.get("artifact_id") if trace_context else None,
                          payload={"pushed": success, "local_head": local_head, "deploy_sha": latest_deploy_sha})

    def _handle_gmail_digest(self, decision: TickDecision, trace_context: dict[str, Any] | None = None) -> None:
        """Gmail digest — requires active MCP session for full access."""
        self.observe.emit("kairos_gmail_digest",
                          trace_id=trace_context.get("trace_id") if trace_context else None,
                          root_trace_id=trace_context.get("root_trace_id") if trace_context else None,
                          span_id=trace_context.get("span_id") if trace_context else None,
                          parent_span_id=trace_context.get("parent_span_id") if trace_context else None,
                          job_id=trace_context.get("job_id") if trace_context else None,
                          artifact_id=trace_context.get("artifact_id") if trace_context else None,
                          payload={"status": "requires_session", "note": "Gmail MCP needs active Claude session"})
        logger.info("KAIROS gmail_digest: emitted — requires active session for full access")

    def _handle_nlm_wiki_sync(self, decision: TickDecision, trace_context: dict[str, Any] | None = None) -> None:
        """Extract knowledge from NotebookLM and ingest into wiki."""
        if self.wiki is None:
            raise RuntimeError("WikiService not configured")
        if self.nlm_service is None:
            raise RuntimeError("NotebookLM service not configured")
        result = self.wiki.ingest_from_notebooklm(self.nlm_service)
        logger.info("KAIROS nlm_wiki_sync: notebooks=%d pages=%d",
                     result.get("notebooks_scanned", 0), result.get("pages_written", 0))
        self.observe.emit("kairos_nlm_wiki_sync",
                          trace_id=trace_context.get("trace_id") if trace_context else None,
                          root_trace_id=trace_context.get("root_trace_id") if trace_context else None,
                          span_id=trace_context.get("span_id") if trace_context else None,
                          parent_span_id=trace_context.get("parent_span_id") if trace_context else None,
                          job_id=trace_context.get("job_id") if trace_context else None,
                          artifact_id=trace_context.get("artifact_id") if trace_context else None,
                          payload=result)

    def _handle_generate_skill(self, decision: TickDecision, trace_context: dict[str, Any] | None = None) -> None:
        """Memento-Skills: generate a new skill from description."""
        if self.skill_registry is None:
            raise RuntimeError("SkillRegistry not configured")
        task_desc = decision.detail or "general utility skill"
        result = self.skill_registry.generate_skill(task_description=task_desc)
        logger.info("KAIROS generate_skill: success=%s name=%s",
                     result.get("success"), result.get("name", ""))
        self.observe.emit("kairos_generate_skill",
                          trace_id=trace_context.get("trace_id") if trace_context else None,
                          root_trace_id=trace_context.get("root_trace_id") if trace_context else None,
                          span_id=trace_context.get("span_id") if trace_context else None,
                          parent_span_id=trace_context.get("parent_span_id") if trace_context else None,
                          job_id=trace_context.get("job_id") if trace_context else None,
                          artifact_id=trace_context.get("artifact_id") if trace_context else None,
                          payload=result)

    def _handle_a2a_send(self, decision: TickDecision, trace_context: dict[str, Any] | None = None) -> None:
        """A2A: send a task to a peer agent."""
        if self.a2a is None:
            raise RuntimeError("A2AService not configured")
        try:
            params = json.loads(decision.detail) if decision.detail else {}
        except (json.JSONDecodeError, TypeError):
            params = {"to_agent": str(decision.detail), "action": "generic", "payload": {}}
        result = self.a2a.send_task(
            to_agent=params.get("to_agent", ""),
            action=params.get("action", ""),
            payload=params.get("payload", {}),
        )
        logger.info("KAIROS a2a_send: success=%s to=%s", result.get("success"), params.get("to_agent"))
        self.observe.emit("kairos_a2a_send",
                          trace_id=trace_context.get("trace_id") if trace_context else None,
                          root_trace_id=trace_context.get("root_trace_id") if trace_context else None,
                          span_id=trace_context.get("span_id") if trace_context else None,
                          parent_span_id=trace_context.get("parent_span_id") if trace_context else None,
                          job_id=trace_context.get("job_id") if trace_context else None,
                          artifact_id=trace_context.get("artifact_id") if trace_context else None,
                          payload=result)

    def _handle_morning_video_brief(self, decision: TickDecision, trace_context: dict[str, Any] | None = None) -> None:
        """Generate daily briefing video via HeyGen and notify user."""
        import datetime

        # 1. Generate brief text via alma sub-agent
        if self.sub_agents is None:
            raise RuntimeError("Sub-agent service not configured")
        brief_text = self.sub_agents.run_skill("alma", "daily-brief", "", lane="worker")
        if isinstance(brief_text, dict):
            brief_text = brief_text.get("result", brief_text.get("content", str(brief_text)))
        brief_text = str(brief_text)[:4500]  # HeyGen limit is 5000 chars

        # 2. Get HeyGen API key from Keychain
        key_result = subprocess.run(
            ["security", "find-generic-password", "-a", "heygen", "-s", "HEYGEN_API_KEY", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        api_key = key_result.stdout.strip()
        if not api_key:
            raise RuntimeError("HEYGEN_API_KEY not found in Keychain")

        # 3. Submit video generation
        payload = json.dumps({
            "video_inputs": [{
                "character": {"type": "avatar", "avatar_id": "284630e731f04f49ae7ba9f5d839e6bb", "avatar_style": "normal"},
                "voice": {"type": "text", "input_text": brief_text, "voice_id": "398936ac428244c6966feefe6d151c6a"},
            }],
            "title": f"Morning Brief — {datetime.date.today().isoformat()}",
            "dimension": {"width": 1280, "height": 720},
        }).encode()
        req = Request(
            "https://api.heygen.com/v2/video/generate",
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Api-Key": api_key,
            },
            method="POST",
        )
        with urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        video_id = body.get("data", {}).get("video_id", "")
        if not video_id:
            raise RuntimeError(f"HeyGen video creation failed: {body}")

        # 4. Poll for completion in background thread to avoid blocking Kairos tick loop
        def _poll_heygen():
            video_url = ""
            try:
                for _ in range(10):
                    time.sleep(30)
                    status_req = Request(
                        f"https://api.heygen.com/v1/video_status.get?video_id={video_id}",
                        headers={"Accept": "application/json", "X-Api-Key": api_key},
                    )
                    with urlopen(status_req, timeout=15) as status_resp:
                        status_body = json.loads(status_resp.read())
                    status = status_body.get("data", {}).get("status", "")
                    if status == "completed":
                        video_url = status_body.get("data", {}).get("video_url", "")
                        break
                    if status == "failed":
                        logger.error("HeyGen video failed: %s", status_body)
                        return
            except Exception:
                logger.exception("HeyGen polling error for video_id=%s", video_id)
                return
            message = f"🎬 Morning Brief listo: {video_url}" if video_url else f"Video en proceso (id: {video_id})"
            self.observe.emit(
                "kairos_notify_user",
                trace_id=trace_context.get("trace_id") if trace_context else None,
                root_trace_id=trace_context.get("root_trace_id") if trace_context else None,
                span_id=trace_context.get("span_id") if trace_context else None,
                parent_span_id=trace_context.get("parent_span_id") if trace_context else None,
                job_id=trace_context.get("job_id") if trace_context else None,
                artifact_id=trace_context.get("artifact_id") if trace_context else None,
                payload={"message": message, "video_url": video_url, "video_id": video_id},
            )
            logger.info("KAIROS morning_video_brief: video_id=%s url=%s", video_id, video_url[:50] if video_url else "pending")

        import threading
        threading.Thread(target=_poll_heygen, daemon=True, name=f"heygen-poll-{video_id[:8]}").start()
