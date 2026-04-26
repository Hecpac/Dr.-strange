from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable


NotifyFn = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class AlertRule:
    title: str
    severity: str = "warning"
    cooldown_seconds: float = 3600.0


DEFAULT_ALERT_RULES: dict[str, AlertRule] = {
    "firecrawl_paused": AlertRule("Firecrawl paused", severity="critical", cooldown_seconds=6 * 3600),
    "wiki_scrape_skipped": AlertRule("Wiki scrape skipped", cooldown_seconds=3600),
    "scheduled_job_error": AlertRule("Scheduled job failed", severity="critical", cooldown_seconds=1800),
    "daemon_tick_error": AlertRule("Daemon tick failed", severity="critical", cooldown_seconds=1800),
    "daemon_task_reconciliation": AlertRule("Stale task reconciliation", cooldown_seconds=1800),
    "session_resume_failed": AlertRule("Provider session resume failed", cooldown_seconds=1800),
    "llm_circuit_open": AlertRule("LLM provider circuit opened", severity="critical", cooldown_seconds=1800),
    "auto_research_adapter_error": AlertRule("Auto-research provider failure", severity="critical", cooldown_seconds=1800),
    "pipeline_poll_degraded": AlertRule("Pipeline poll degraded", cooldown_seconds=1800),
    "telegram_transport_stop_error": AlertRule("Telegram transport stop degraded", cooldown_seconds=1800),
    "nlm_research_degraded": AlertRule("NotebookLM degraded", cooldown_seconds=1800),
    "nlm_research_failed": AlertRule("NotebookLM failed", severity="critical", cooldown_seconds=1800),
}


class OperationalAlertRouter:
    """Forward actionable observe events to an operator notification channel."""

    def __init__(
        self,
        *,
        observe: Any,
        notify: NotifyFn,
        rules: dict[str, AlertRule] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.observe = observe
        self.notify = notify
        self.rules = dict(rules or DEFAULT_ALERT_RULES)
        self.clock = clock or time.time
        self._last_sent: dict[str, float] = {}

    def install(self) -> None:
        for event_type in self.rules:
            self.observe.subscribe(
                event_type,
                lambda payload, event_type=event_type: self.handle(event_type, payload),
            )

    def handle(self, event_type: str, payload: dict[str, Any] | None = None) -> bool:
        event_payload = payload or {}
        rule = self.rules.get(event_type)
        if rule is None:
            return False
        if event_payload.get("user_notified") is True:
            self._emit_status("operational_alert_suppressed", event_type, event_payload, reason="user_notified")
            return False
        dedupe_key = _dedupe_key(event_type, event_payload)
        now = self.clock()
        last_sent = self._last_sent.get(dedupe_key)
        if last_sent is not None and now - last_sent < rule.cooldown_seconds:
            self._emit_status("operational_alert_suppressed", event_type, event_payload, reason="cooldown")
            return False
        message = _format_alert(event_type, rule, event_payload)
        self.notify(message)
        self._last_sent[dedupe_key] = now
        self._emit_status("operational_alert_sent", event_type, event_payload, reason="")
        return True

    def _emit_status(self, status_event: str, event_type: str, payload: dict[str, Any], *, reason: str) -> None:
        try:
            self.observe.emit(
                status_event,
                payload={
                    "event_type": event_type,
                    "reason": reason,
                    "source_payload": _compact_payload(payload),
                },
            )
        except Exception:
            pass


def install_operational_alerts(*, observe: Any, notify: NotifyFn) -> OperationalAlertRouter:
    router = OperationalAlertRouter(observe=observe, notify=notify)
    router.install()
    return router


def _format_alert(event_type: str, rule: AlertRule, payload: dict[str, Any]) -> str:
    detail = _event_detail(event_type, payload)
    lines = [
        f"Alerta operacional: {rule.title}",
        f"Severidad: {rule.severity}",
    ]
    if detail:
        lines.append(detail)
    return "\n".join(lines)


def _event_detail(event_type: str, payload: dict[str, Any]) -> str:
    if event_type == "firecrawl_paused":
        reason = payload.get("reason", "unknown")
        paused_seconds = payload.get("paused_seconds", "?")
        return f"Reason: {reason}\nPaused seconds: {paused_seconds}"
    if event_type == "wiki_scrape_skipped":
        return f"Reason: {payload.get('reason', 'unknown')}\nDetail: {payload.get('detail', '')}"
    if event_type == "scheduled_job_error":
        return f"Job: {payload.get('job', 'unknown')}\nError: {payload.get('error', 'unknown')}"
    if event_type == "daemon_tick_error":
        return f"Error: {payload.get('error', 'unknown')}"
    if event_type == "daemon_task_reconciliation":
        return f"Lost tasks: {payload.get('lost_tasks', 0)}"
    if event_type == "session_resume_failed":
        return (
            f"Provider: {payload.get('provider', payload.get('lane', 'unknown'))}\n"
            f"Session: {payload.get('stale_session', 'unknown')}\n"
            f"Error: {payload.get('error', 'unknown')}"
        )
    if event_type == "llm_circuit_open":
        return (
            f"Provider: {payload.get('provider', 'unknown')}\n"
            f"Failures: {payload.get('failures', '?')}\n"
            f"Reason: {payload.get('reason', 'unknown')}"
        )
    if event_type == "auto_research_adapter_error":
        return (
            f"Agent: {payload.get('agent', 'unknown')}\n"
            f"Reason: {payload.get('reason', 'unknown')}\n"
            f"Failures: {payload.get('consecutive_failures', '?')}\n"
            f"Error: {payload.get('error', 'unknown')}"
        )
    if event_type == "perf_optimizer_paused":
        return (
            f"Reason: {payload.get('reason', 'unknown')}\n"
            f"Experiments run: {payload.get('experiments', 0)}"
        )
    if event_type == "pipeline_poll_degraded":
        return (
            f"Poller: {payload.get('poller', 'unknown')}\n"
            f"Reason: {payload.get('reason', 'unknown')}\n"
            f"Failures: {payload.get('consecutive_failures', '?')}\n"
            f"Backoff seconds: {payload.get('backoff_seconds', '?')}\n"
            f"Error: {payload.get('error', 'unknown')}"
        )
    if event_type == "telegram_transport_stop_error":
        errors = payload.get("errors") or []
        first_error = errors[0] if isinstance(errors, list) and errors else payload.get("error", "unknown")
        return (
            f"Errors: {payload.get('error_count', len(errors) if isinstance(errors, list) else '?')}\n"
            f"First error: {first_error}"
        )
    if event_type in {"nlm_research_degraded", "nlm_research_failed"}:
        return (
            f"Reason: {payload.get('reason') or payload.get('failure_kind') or 'unknown'}\n"
            f"Notebook: {payload.get('notebook_id', 'unknown')}\n"
            f"Fallback used: {payload.get('fallback_used', False)}"
        )
    return _compact_payload(payload)


def _dedupe_key(event_type: str, payload: dict[str, Any]) -> str:
    parts = [
        event_type,
        str(payload.get("job") or payload.get("agent") or payload.get("poller") or payload.get("reason") or payload.get("failure_kind") or ""),
        str(payload.get("notebook_id") or payload.get("stale_session") or payload.get("error") or "")[:120],
    ]
    return ":".join(parts)


def _compact_payload(payload: dict[str, Any]) -> str:
    try:
        raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    except TypeError:
        raw = str(payload)
    return raw[:1000]
