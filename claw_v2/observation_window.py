from __future__ import annotations

import json
import logging
import shlex
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

NotifyCallback = Callable[[str], None]


class ObservationWindowBlocked(PermissionError):
    """Raised when the observation window blocks autonomous execution."""


@dataclass(slots=True)
class ObservationWindowConfig:
    cost_per_hour_threshold: float = 1.50
    tool_calls_per_minute_threshold: int = 10
    daily_budget_cap: float | None = None


def hard_denylist_reason(tool_name: str, args: dict[str, Any]) -> str | None:
    if tool_name != "Bash":
        return None
    command = str(args.get("command") or args.get("cmd") or "").strip()
    if not command:
        return None
    tokens = _shell_tokens(command)
    if not tokens:
        return None
    if _has_git_force_push(tokens):
        return "hard denylist: git push --force"
    if _has_vercel_prod(tokens):
        return "hard denylist: vercel --prod"
    if _has_gh_release_create(tokens):
        return "hard denylist: gh release create"
    if _has_dynamic_rm_rf(tokens):
        return "hard denylist: rm -rf with dynamic arguments"
    return None


class ObservationWindowState:
    def __init__(
        self,
        *,
        observe: object | None = None,
        state_path: Path | str | None = None,
        config: ObservationWindowConfig | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.observe = observe
        self.config = config or ObservationWindowConfig()
        self.state_path = Path(state_path).expanduser() if state_path is not None else Path.home() / ".claw" / "observation_window.json"
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._tool_call_times: deque[float] = deque()
        self._llm_costs: deque[tuple[float, float]] = deque()
        self._tripped_breakers: set[str] = set()
        self._alert_notifier: NotifyCallback | None = None
        self._stream_notifier: NotifyCallback | None = None
        self._frozen = False
        self._freeze_reason = ""
        self._freeze_actor = ""
        self._load_state()

    @property
    def frozen(self) -> bool:
        with self._lock:
            return self._frozen

    @property
    def freeze_reason(self) -> str:
        with self._lock:
            return self._freeze_reason

    def set_alert_notifier(self, notifier: NotifyCallback | None) -> None:
        self._alert_notifier = notifier

    def set_stream_notifier(self, notifier: NotifyCallback | None) -> None:
        self._stream_notifier = notifier

    def freeze(self, *, reason: str, actor: str = "system") -> None:
        with self._lock:
            changed = not self._frozen or self._freeze_reason != reason
            self._frozen = True
            self._freeze_reason = reason
            self._freeze_actor = actor
            self._persist_state_locked()
        if changed:
            self._emit(
                "observation_window_freeze_set",
                {"reason": reason, "actor": actor},
            )
            self._notify_alert(f"Observation window frozen: {reason}")

    def unfreeze(self, *, actor: str = "system") -> None:
        with self._lock:
            was_frozen = self._frozen
            self._frozen = False
            self._freeze_reason = ""
            self._freeze_actor = actor
            self._tripped_breakers.clear()
            self._persist_state_locked()
        if was_frozen:
            self._emit("observation_window_freeze_cleared", {"actor": actor})
            self._notify_alert(f"Observation window unfrozen by {actor}")

    def before_tool_execution(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        tier: int,
        actor: str,
    ) -> None:
        reason = hard_denylist_reason(tool_name, args)
        if reason is not None:
            payload = {"tool": tool_name, "tier": tier, "actor": actor, "reason": reason}
            self._emit("tool_hard_denylist_blocked", payload)
            self._notify_alert(f"Blocked hard-denylisted tool: {tool_name} ({reason})")
            raise ObservationWindowBlocked(reason)
        with self._lock:
            if self._frozen:
                reason = self._freeze_reason or "observation_window_frozen"
                payload = {"tool": tool_name, "tier": tier, "actor": actor, "reason": reason}
                self._emit("tool_blocked_by_freeze", payload)
                raise ObservationWindowBlocked(f"observation window frozen: {reason}")
            now = self._clock()
            self._tool_call_times.append(now)
            self._prune_locked(now)
            tool_calls = len(self._tool_call_times)
        if tool_calls > self.config.tool_calls_per_minute_threshold:
            self.trip_breaker(
                "tool_calls_per_minute",
                value=float(tool_calls),
                threshold=float(self.config.tool_calls_per_minute_threshold),
                actor=actor,
            )
            raise ObservationWindowBlocked(
                f"tool_calls_per_minute breaker tripped: {tool_calls} > {self.config.tool_calls_per_minute_threshold}"
            )

    def after_tool_execution(
        self,
        *,
        tool_name: str,
        tier: int,
        actor: str,
        status: str,
        cost: float = 0.0,
        error: str | None = None,
    ) -> None:
        if tier < 2 and status == "ok":
            return
        payload = {
            "tool": tool_name,
            "tier": tier,
            "actor": actor,
            "status": status,
            "cost_estimate": cost,
        }
        if error:
            payload["error"] = error[:300]
        self._emit("observation_tool_event", payload)
        self._notify_stream(_format_stream_line(tool=tool_name, tier=f"tier_{tier}", actor=actor, cost=cost, status=status))

    def handle_llm_audit_event(self, event: dict[str, Any]) -> None:
        action = str(event.get("action") or "llm_event")
        cost = _coerce_float(event.get("cost_estimate"), 0.0)
        now = self._clock()
        if cost > 0:
            with self._lock:
                self._llm_costs.append((now, cost))
                self._prune_locked(now)
                cost_per_hour = sum(item_cost for _, item_cost in self._llm_costs)
            if cost_per_hour > self.config.cost_per_hour_threshold:
                self.trip_breaker(
                    "cost_per_hour",
                    value=cost_per_hour,
                    threshold=self.config.cost_per_hour_threshold,
                    actor=str(event.get("lane") or "llm"),
                )
        if bool(event.get("degraded_mode")):
            self._notify_stream(
                _format_stream_line(
                    tool=action,
                    tier="n/a",
                    actor=str(event.get("lane") or "llm"),
                    cost=cost,
                    status="ok",
                )
            )

    def trip_breaker(self, name: str, *, value: float, threshold: float, actor: str) -> None:
        with self._lock:
            first_trip = name not in self._tripped_breakers
            self._tripped_breakers.add(name)
        payload = {
            "breaker": name,
            "value": round(value, 6),
            "threshold": threshold,
            "actor": actor,
        }
        self._emit("circuit_breaker_tripped", payload)
        self.freeze(reason=f"circuit_breaker:{name}", actor=actor)
        if first_trip:
            self._notify_alert(f"Circuit breaker tripped: {name} value={value:.3f} threshold={threshold:.3f}")

    def status_payload(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            tool_calls = len(self._tool_call_times)
            rolling_cost = round(sum(cost for _, cost in self._llm_costs), 6)
            frozen = self._frozen
            reason = self._freeze_reason
            actor = self._freeze_actor
            tripped = sorted(self._tripped_breakers)
        cost_today = self._cost_today()
        daily_budget = self.config.daily_budget_cap
        remaining = None if daily_budget is None else round(max(daily_budget - cost_today, 0.0), 6)
        recent_events = self._recent_events(limit=200)
        failure_rate = _recent_failure_rate(recent_events)
        active_goal_id = _active_goal_id(recent_events)
        return {
            "frozen": frozen,
            "freeze_reason": reason,
            "freeze_actor": actor,
            "cost_today": round(cost_today, 6),
            "daily_budget_cap": daily_budget,
            "daily_budget_remaining": remaining,
            "rolling_cost_per_hour": rolling_cost,
            "actions_per_minute": tool_calls,
            "recent_failure_rate": failure_rate,
            "active_goal_id": active_goal_id,
            "tripped_breakers": tripped,
            "thresholds": {
                "cost_per_hour": self.config.cost_per_hour_threshold,
                "tool_calls_per_minute": self.config.tool_calls_per_minute_threshold,
            },
        }

    def events_payload(self, *, limit: int = 50, event_type: str | None = None) -> dict[str, Any]:
        events = self._recent_events(limit=max(limit * 4, limit))
        if event_type:
            events = [event for event in events if event.get("event_type") == event_type]
        return {"events": events[:limit]}

    def _cost_today(self) -> float:
        if self.observe is None:
            return 0.0
        try:
            spending = self.observe.spending_today()  # type: ignore[attr-defined]
            return float(spending.get("total") or 0.0)
        except Exception:
            try:
                return float(self.observe.total_cost_today())  # type: ignore[attr-defined]
            except Exception:
                return 0.0

    def _recent_events(self, *, limit: int) -> list[dict[str, Any]]:
        if self.observe is None:
            return []
        try:
            return list(self.observe.recent_events(limit=limit))  # type: ignore[attr-defined]
        except Exception:
            return []

    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        self._frozen = bool(data.get("frozen"))
        self._freeze_reason = str(data.get("reason") or "")
        self._freeze_actor = str(data.get("actor") or "")

    def _persist_state_locked(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "frozen": self._frozen,
            "reason": self._freeze_reason,
            "actor": self._freeze_actor,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.state_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _prune_locked(self, now: float) -> None:
        while self._tool_call_times and now - self._tool_call_times[0] > 60:
            self._tool_call_times.popleft()
        while self._llm_costs and now - self._llm_costs[0][0] > 3600:
            self._llm_costs.popleft()

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.observe is None:
            return
        try:
            self.observe.emit(event_type, payload=payload)  # type: ignore[attr-defined]
        except Exception:
            logger.debug("observation window emit failed", exc_info=True)

    def _notify_alert(self, message: str) -> None:
        if self._alert_notifier is None:
            return
        try:
            self._alert_notifier(message)
        except Exception:
            logger.debug("observation alert notifier failed", exc_info=True)

    def _notify_stream(self, message: str) -> None:
        if self._stream_notifier is None:
            return
        try:
            self._stream_notifier(message)
        except Exception:
            logger.debug("observation stream notifier failed", exc_info=True)


def _format_stream_line(*, tool: str, tier: str, actor: str, cost: float, status: str) -> str:
    hhmm = datetime.now().strftime("%H:%M")
    return f"[{hhmm}] tool={tool} tier={tier} actor={actor} cost=${cost:.4f} status={status}"


def _shell_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _has_git_force_push(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if Path(token).name != "git":
            continue
        tail = tokens[index + 1 :]
        if "push" not in tail:
            continue
        push_index = tail.index("push")
        push_tail = tail[push_index + 1 :]
        return "--force" in push_tail or "-f" in push_tail
    return False


def _has_vercel_prod(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if Path(token).name == "vercel" and "--prod" in tokens[index + 1 :]:
            return True
    return False


def _has_gh_release_create(tokens: list[str]) -> bool:
    for index in range(0, max(len(tokens) - 2, 0)):
        if Path(tokens[index]).name == "gh" and tokens[index + 1 : index + 3] == ["release", "create"]:
            return True
    return False


def _has_dynamic_rm_rf(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if Path(token).name != "rm":
            continue
        flags: list[str] = []
        targets: list[str] = []
        for item in tokens[index + 1 :]:
            if item == "--":
                continue
            if item.startswith("-") and not targets:
                flags.append(item)
                continue
            if item in {"&&", ";", "||", "|"}:
                break
            targets.append(item)
        compact_flags = "".join(flag.lstrip("-") for flag in flags)
        if "r" not in compact_flags or "f" not in compact_flags:
            continue
        if any(_is_dynamic_rm_target(target) for target in targets):
            return True
    return False


def _is_dynamic_rm_target(target: str) -> bool:
    dynamic_tokens = ("$", "`", "*", "?", "[", "]", "{", "}", "$(")
    return any(marker in target for marker in dynamic_tokens)


def _recent_failure_rate(events: list[dict[str, Any]]) -> float:
    finished = [
        event for event in events
        if event.get("event_type") in {"action_executed", "action_failed"}
    ][:50]
    if not finished:
        return 0.0
    failures = sum(1 for event in finished if event.get("event_type") == "action_failed")
    return round(failures / len(finished), 3)


def _active_goal_id(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        payload = event.get("payload") or {}
        goal_id = payload.get("goal_id")
        if isinstance(goal_id, str) and goal_id:
            return goal_id
    return None


def _coerce_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
