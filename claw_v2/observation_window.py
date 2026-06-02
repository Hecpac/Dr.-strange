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

# Cost-per-hour breaker is an LLM-spend signal. It must not block safe local
# read-only inspection — operators (and the bot) still need Read/Grep/Glob to
# diagnose what is happening. Higher tiers stay gated.
LOCAL_READ_ONLY_TIER = 1


class ObservationWindowBlocked(PermissionError):
    """Raised when the observation window blocks autonomous execution."""


@dataclass(slots=True)
class ObservationWindowConfig:
    cost_per_hour_threshold: float = 10.00
    tool_calls_per_minute_threshold: int = 10
    daily_budget_cap: float | None = None
    stale_freeze_seconds: float = 3600.0
    notional_cost_providers: tuple[str, ...] = ()
    token_window_seconds: int = 18_000
    token_window_cap: int = 1_000_000
    token_soft_limit_ratio: float = 0.8
    token_hard_limit_ratio: float = 1.0


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
        self._llm_tokens: deque[tuple[float, int, bool]] = deque()
        self._tripped_breakers: set[str] = set()
        self._alert_notifier: NotifyCallback | None = None
        self._stream_notifier: NotifyCallback | None = None
        self._frozen = False
        self._freeze_reason = ""
        self._freeze_actor = ""
        self._freeze_updated_at: float | None = None
        self._token_soft_limit_active = False
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
            self._freeze_updated_at = self._clock()
            self._persist_state_locked()
        if changed:
            self._emit(
                "observation_window_freeze_set",
                {"reason": reason, "actor": actor},
            )
            if not _diagnostic_only_freeze_reason(reason):
                self._notify_alert(f"Observation window frozen: {reason}")

    def unfreeze(self, *, actor: str = "system") -> None:
        with self._lock:
            was_frozen = self._frozen
            self._frozen = False
            self._freeze_reason = ""
            self._freeze_actor = actor
            self._freeze_updated_at = self._clock()
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
            now = self._clock()
            self._prune_locked(now)
            auto_cleared = self._clear_token_window_freeze_if_decayed_locked(now)
            frozen = self._frozen
            freeze_reason = self._freeze_reason or "observation_window_frozen"
        if auto_cleared:
            self._emit("observation_window_freeze_auto_cleared", {"stale_reason": "circuit_breaker:token_window"})
        if frozen:
            if _allows_read_only_during_freeze(freeze_reason) and tier <= LOCAL_READ_ONLY_TIER:
                self._emit(
                    "tool_allowed_during_cost_breaker",
                    {
                        "tool": tool_name,
                        "tier": tier,
                        "actor": actor,
                        "freeze_reason": freeze_reason,
                    },
                )
            else:
                payload = {"tool": tool_name, "tier": tier, "actor": actor, "reason": freeze_reason}
                self._emit("tool_blocked_by_freeze", payload)
                raise ObservationWindowBlocked(f"observation window frozen: {freeze_reason}")
        with self._lock:
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
        provider = str(event.get("provider") or "")
        self._record_token_window_event(event, now=now)
        if cost > 0 and provider in set(self.config.notional_cost_providers):
            self._emit(
                "llm_notional_cost_ignored",
                {
                    "lane": event.get("lane"),
                    "provider": provider,
                    "model": event.get("model"),
                    "cost_estimate": cost,
                },
            )
            cost = 0.0
        if cost > 0:
            with self._lock:
                self._llm_costs.append((now, cost))
                self._prune_locked(now)
                cost_per_hour = sum(item_cost for _, item_cost in self._llm_costs)
            if cost_per_hour > self.config.cost_per_hour_threshold:
                lane = str(event.get("lane")) if event.get("lane") is not None else None
                provider_for_event = str(event.get("provider")) if event.get("provider") is not None else None
                self.trip_breaker(
                    "cost_per_hour",
                    value=cost_per_hour,
                    threshold=self.config.cost_per_hour_threshold,
                    actor=lane or "llm",
                    lane=lane,
                    provider=provider_for_event,
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

    def before_llm_request(
        self,
        *,
        lane: str,
        provider: str,
        model: str,
        estimated_input_tokens: int = 0,
    ) -> None:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            auto_cleared = self._clear_token_window_freeze_if_decayed_locked(now)
            frozen = self._frozen
            freeze_reason = self._freeze_reason or "observation_window_frozen"
            totals = self._token_window_totals_locked()
            compact_before_large_calls = totals["total_tokens"] >= self._token_soft_threshold()
        if auto_cleared:
            self._emit("observation_window_freeze_auto_cleared", {"stale_reason": "circuit_breaker:token_window"})
        if frozen and freeze_reason == "circuit_breaker:token_window":
            payload = {
                "lane": lane,
                "provider": provider,
                "model": model,
                "reason": freeze_reason,
                "rolling_tokens": totals["total_tokens"],
                "token_window_cap": self.config.token_window_cap,
            }
            self._emit("llm_blocked_by_token_window", payload)
            raise ObservationWindowBlocked("observation window frozen: circuit_breaker:token_window")
        if compact_before_large_calls:
            self._emit(
                "token_window_compaction_recommended",
                {
                    "lane": lane,
                    "provider": provider,
                    "model": model,
                    "rolling_tokens": totals["total_tokens"],
                    "estimated_request_tokens": max(int(estimated_input_tokens), 0),
                    "soft_limit": self._token_soft_threshold(),
                },
            )

    def trip_breaker(
        self,
        name: str,
        *,
        value: float,
        threshold: float,
        actor: str,
        lane: str | None = None,
        provider: str | None = None,
    ) -> None:
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
        if name == "cost_per_hour":
            degraded_payload: dict[str, Any] = {
                "actor": actor,
                "value": round(value, 6),
                "threshold": threshold,
                "allowed_capabilities": ["tier_1_local_read_only"],
                "blocked_capabilities": [
                    "llm_calls_until_window_decays",
                    "tier_2_local_mutation",
                    "tier_3_external_or_approval_required",
                ],
            }
            if lane is not None:
                degraded_payload["lane"] = lane
            if provider is not None:
                degraded_payload["provider"] = provider
            self._emit("autonomy_degraded_by_cost_breaker", degraded_payload)
        elif name == "token_window":
            degraded_payload = {
                "actor": actor,
                "value": round(value, 6),
                "threshold": threshold,
                "allowed_capabilities": ["tier_1_local_read_only"],
                "blocked_capabilities": [
                    "large_llm_calls_until_window_decays",
                    "tier_2_local_mutation",
                    "tier_3_external_or_approval_required",
                ],
            }
            if lane is not None:
                degraded_payload["lane"] = lane
            if provider is not None:
                degraded_payload["provider"] = provider
            self._emit("autonomy_degraded_by_token_window", degraded_payload)
        if first_trip:
            logger.warning(
                "Circuit breaker tripped: %s value=%.3f threshold=%.3f actor=%s",
                name,
                value,
                threshold,
                actor,
            )
            self._notify_stream(
                f"[diagnostic] circuit_breaker={name} value={value:.3f} "
                f"threshold={threshold:.3f} frozen=true"
            )

    def status_payload(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            auto_cleared = self._clear_token_window_freeze_if_decayed_locked(now)
            tool_calls = len(self._tool_call_times)
            rolling_cost = round(sum(cost for _, cost in self._llm_costs), 6)
            token_window = self._token_window_payload_locked()
            frozen = self._frozen
            reason = self._freeze_reason
            actor = self._freeze_actor
            tripped = sorted(self._tripped_breakers)
        if auto_cleared:
            self._emit("observation_window_freeze_auto_cleared", {"stale_reason": "circuit_breaker:token_window"})
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
            "token_window": token_window,
            "actions_per_minute": tool_calls,
            "recent_failure_rate": failure_rate,
            "active_goal_id": active_goal_id,
            "tripped_breakers": tripped,
            "thresholds": {
                "cost_per_hour": self.config.cost_per_hour_threshold,
                "tool_calls_per_minute": self.config.tool_calls_per_minute_threshold,
                "token_window_cap": self.config.token_window_cap,
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
            notional = set(self.config.notional_cost_providers)
            if notional:
                rows = spending.get("rows") or []
                return sum(
                    float(row.get("cost") or 0.0)
                    for row in rows
                    if str(row.get("provider") or "") not in notional
                )
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
        self._freeze_updated_at = _timestamp_from_iso(data.get("updated_at"))
        # circuit_breaker:* freezes are backed by rolling-window evidence (1h cost,
        # 1m tool-call rate). Once the window has decayed past the TTL, the freeze
        # is no longer evidence-backed; auto-clear so a restart isn't permanently
        # bricked. Manual freezes (manual_*) always require explicit unfreeze.
        if self._frozen and self._freeze_reason.startswith("circuit_breaker:"):
            age = _freeze_age_seconds(data.get("updated_at"))
            ttl_seconds = self._freeze_ttl_seconds(self._freeze_reason)
            if age > ttl_seconds:
                stale_reason = self._freeze_reason
                stale_actor = self._freeze_actor
                self._frozen = False
                self._freeze_reason = ""
                self._freeze_actor = "auto_clear_stale"
                self._freeze_updated_at = self._clock()
                self._persist_state_locked()
                logger.warning(
                    "Auto-cleared stale circuit_breaker freeze: reason=%s prior_actor=%s age=%.0fs ttl=%.0fs",
                    stale_reason,
                    stale_actor,
                    age,
                    ttl_seconds,
                )
                self._emit(
                    "observation_window_freeze_auto_cleared",
                    {
                        "stale_reason": stale_reason,
                        "stale_actor": stale_actor,
                        "age_seconds": round(age, 1),
                        "ttl_seconds": ttl_seconds,
                    },
                )

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
        while self._llm_tokens and now - self._llm_tokens[0][0] > self.config.token_window_seconds:
            self._llm_tokens.popleft()
        self._token_soft_limit_active = self._token_window_totals_locked()["total_tokens"] >= self._token_soft_threshold()

    def _record_token_window_event(self, event: dict[str, Any], *, now: float) -> None:
        token_usage = _extract_token_usage(event)
        tokens = int(token_usage.get("total_tokens") or 0)
        if tokens <= 0:
            return
        estimated = bool(token_usage.get("estimated"))
        lane = str(event.get("lane") or "llm")
        provider = str(event.get("provider") or "")
        model = str(event.get("model") or "")
        with self._lock:
            self._prune_locked(now)
            was_soft_active = self._token_soft_limit_active
            self._llm_tokens.append((now, tokens, estimated))
            self._prune_locked(now)
            totals = self._token_window_totals_locked()
            soft_limit = self._token_soft_threshold()
            hard_limit = self._token_hard_threshold()
            soft_crossed = totals["total_tokens"] >= soft_limit and not was_soft_active
            self._token_soft_limit_active = totals["total_tokens"] >= soft_limit
        self._emit(
            "llm_token_window_recorded",
            {
                "lane": lane,
                "provider": provider,
                "model": model,
                "tokens": tokens,
                "estimated": estimated,
                "rolling_tokens": totals["total_tokens"],
                "estimated_tokens": totals["estimated_tokens"],
                "real_tokens": totals["real_tokens"],
                "token_window_seconds": self.config.token_window_seconds,
                "token_window_cap": self.config.token_window_cap,
            },
        )
        if soft_crossed:
            self._emit(
                "token_window_soft_limit_reached",
                {
                    "lane": lane,
                    "provider": provider,
                    "model": model,
                    "rolling_tokens": totals["total_tokens"],
                    "soft_limit": soft_limit,
                    "token_window_cap": self.config.token_window_cap,
                    "compact_before_large_calls": True,
                },
            )
            self._notify_alert(
                f"Token window soft limit reached: {totals['total_tokens']} >= {soft_limit}. "
                "Compact before large LLM calls."
            )
        if totals["total_tokens"] >= hard_limit:
            self.trip_breaker(
                "token_window",
                value=float(totals["total_tokens"]),
                threshold=float(hard_limit),
                actor=lane or "llm",
                lane=lane,
                provider=provider or None,
            )

    def _token_window_totals_locked(self) -> dict[str, int]:
        total = sum(tokens for _, tokens, _ in self._llm_tokens)
        estimated = sum(tokens for _, tokens, is_estimated in self._llm_tokens if is_estimated)
        real = total - estimated
        return {
            "total_tokens": total,
            "estimated_tokens": estimated,
            "real_tokens": real,
        }

    def _token_window_payload_locked(self) -> dict[str, Any]:
        totals = self._token_window_totals_locked()
        cap = max(int(self.config.token_window_cap), 1)
        soft_limit = self._token_soft_threshold()
        hard_limit = self._token_hard_threshold()
        total = totals["total_tokens"]
        return {
            "window_seconds": int(self.config.token_window_seconds),
            "cap": cap,
            "soft_limit": soft_limit,
            "hard_limit": hard_limit,
            "total_tokens": total,
            "real_tokens": totals["real_tokens"],
            "estimated_tokens": totals["estimated_tokens"],
            "estimated": totals["estimated_tokens"] > 0,
            "usage_ratio": round(total / cap, 6),
            "soft_limit_reached": total >= soft_limit,
            "hard_limit_reached": total >= hard_limit,
            "compact_before_large_calls": total >= soft_limit,
        }

    def _token_soft_threshold(self) -> int:
        return max(int(self.config.token_window_cap * self.config.token_soft_limit_ratio), 1)

    def _token_hard_threshold(self) -> int:
        return max(int(self.config.token_window_cap * self.config.token_hard_limit_ratio), 1)

    def _clear_token_window_freeze_if_decayed_locked(self, now: float) -> bool:
        if self._freeze_reason != "circuit_breaker:token_window":
            return False
        totals = self._token_window_totals_locked()
        if totals["total_tokens"] >= self._token_hard_threshold():
            return False
        if not self._llm_tokens and self._token_window_freeze_age_seconds(now) <= self.config.token_window_seconds:
            return False
        self._frozen = False
        self._freeze_reason = ""
        self._freeze_actor = "auto_clear_token_window"
        self._freeze_updated_at = now
        self._tripped_breakers.discard("token_window")
        self._persist_state_locked()
        return True

    def _freeze_ttl_seconds(self, reason: str) -> float:
        if reason == "circuit_breaker:token_window":
            return float(self.config.token_window_seconds)
        return self.config.stale_freeze_seconds

    def _token_window_freeze_age_seconds(self, now: float) -> float:
        if self._freeze_updated_at is None:
            return float("inf")
        return max(now - self._freeze_updated_at, 0.0)

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


def _is_cost_breaker_reason(reason: str) -> bool:
    """True iff the freeze is purely an LLM cost-per-hour signal."""
    return reason == "circuit_breaker:cost_per_hour"


def _is_token_window_breaker_reason(reason: str) -> bool:
    return reason == "circuit_breaker:token_window"


def _allows_read_only_during_freeze(reason: str) -> bool:
    return _is_cost_breaker_reason(reason) or _is_token_window_breaker_reason(reason)


def _diagnostic_only_freeze_reason(reason: str) -> bool:
    # cost_per_hour is a budget alarm — it must reach the operator (Telegram).
    # token_window is also an autonomy budget alarm, not a diagnostic-only
    # circuit breaker.
    # All other circuit_breaker:* reasons (provider, sdk, etc.) stay diagnostic.
    if reason in {"circuit_breaker:cost_per_hour", "circuit_breaker:token_window"}:
        return False
    return reason.startswith("circuit_breaker:")


def _extract_token_usage(event: dict[str, Any]) -> dict[str, Any]:
    candidates: list[Any] = [event.get("token_usage")]
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        candidates.append(metadata.get("token_usage"))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        total = _coerce_int(candidate.get("total_tokens"), 0)
        if total <= 0:
            continue
        return {
            "total_tokens": total,
            "estimated": bool(candidate.get("estimated")),
        }
    return {"total_tokens": 0, "estimated": True}


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


def _coerce_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _freeze_age_seconds(updated_at_raw: object) -> float:
    if not isinstance(updated_at_raw, str):
        return float("inf")
    try:
        updated_at = datetime.fromisoformat(updated_at_raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return float("inf")
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - updated_at).total_seconds(), 0.0)


def _timestamp_from_iso(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()
