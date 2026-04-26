from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Callable, Literal

RetryAction = Literal["retry", "switch_tool", "ask_user"]
CircuitStatus = Literal["closed", "open", "half_open"]


@dataclass(slots=True)
class RetryDecision:
    action: RetryAction
    reason: str
    failures_for_tool: int
    distinct_failed_tools: int

    @property
    def truly_stuck(self) -> bool:
        return self.action == "ask_user"


@dataclass(slots=True)
class RetryStuckPolicy:
    same_tool_limit: int = 3
    distinct_tool_limit: int = 3
    _tool_failures: dict[str, int] = field(default_factory=dict)

    def record_failure(self, tool_name: str) -> RetryDecision:
        tool = (tool_name or "unknown").strip() or "unknown"
        self._tool_failures[tool] = self._tool_failures.get(tool, 0) + 1
        failures_for_tool = self._tool_failures[tool]
        distinct_failed_tools = len(self._tool_failures)
        if distinct_failed_tools >= self.distinct_tool_limit:
            return RetryDecision(
                action="ask_user",
                reason="failed with three distinct tools",
                failures_for_tool=failures_for_tool,
                distinct_failed_tools=distinct_failed_tools,
            )
        if failures_for_tool >= self.same_tool_limit:
            return RetryDecision(
                action="switch_tool",
                reason="same tool failed three times",
                failures_for_tool=failures_for_tool,
                distinct_failed_tools=distinct_failed_tools,
            )
        return RetryDecision(
            action="retry",
            reason="retry budget remains",
            failures_for_tool=failures_for_tool,
            distinct_failed_tools=distinct_failed_tools,
        )

    def reset_tool(self, tool_name: str) -> None:
        self._tool_failures.pop(tool_name, None)

    def reset(self) -> None:
        self._tool_failures.clear()


@dataclass(slots=True)
class CircuitDecision:
    provider: str
    allowed: bool
    status: CircuitStatus
    failures: int = 0
    opened_until: float = 0.0
    reason: str = ""


@dataclass(slots=True)
class CircuitTransition:
    provider: str
    status: CircuitStatus
    failures: int
    opened_until: float = 0.0
    changed: bool = False
    reason: str = ""


@dataclass(slots=True)
class _ProviderCircuitState:
    failures: int = 0
    opened_until: float = 0.0
    last_error: str = ""


@dataclass(slots=True)
class ProviderCircuitBreaker:
    failure_threshold: int = 3
    cooldown_seconds: float = 120.0
    clock: Callable[[], float] = time.monotonic
    _states: dict[str, _ProviderCircuitState] = field(default_factory=dict)

    def check(self, provider: str) -> CircuitDecision:
        name = _provider_key(provider)
        state = self._states.get(name)
        if state is None or state.failures <= 0:
            return CircuitDecision(provider=name, allowed=True, status="closed")
        now = self.clock()
        if state.opened_until > now:
            return CircuitDecision(
                provider=name,
                allowed=False,
                status="open",
                failures=state.failures,
                opened_until=state.opened_until,
                reason=state.last_error,
            )
        if state.opened_until > 0:
            return CircuitDecision(
                provider=name,
                allowed=True,
                status="half_open",
                failures=state.failures,
                opened_until=state.opened_until,
                reason=state.last_error,
            )
        return CircuitDecision(
            provider=name,
            allowed=True,
            status="closed",
            failures=state.failures,
            reason=state.last_error,
        )

    def record_failure(self, provider: str, error: BaseException | str) -> CircuitTransition:
        name = _provider_key(provider)
        state = self._states.setdefault(name, _ProviderCircuitState())
        was_open = state.opened_until > self.clock()
        state.failures += 1
        state.last_error = str(error)[:500]
        if state.failures >= max(1, self.failure_threshold):
            state.opened_until = self.clock() + max(0.0, self.cooldown_seconds)
            return CircuitTransition(
                provider=name,
                status="open",
                failures=state.failures,
                opened_until=state.opened_until,
                changed=not was_open,
                reason=state.last_error,
            )
        return CircuitTransition(
            provider=name,
            status="closed",
            failures=state.failures,
            reason=state.last_error,
        )

    def record_success(self, provider: str) -> CircuitTransition:
        name = _provider_key(provider)
        state = self._states.get(name)
        if state is None or state.failures <= 0:
            return CircuitTransition(provider=name, status="closed", failures=0)
        was_recovering = state.opened_until > 0
        self._states.pop(name, None)
        return CircuitTransition(
            provider=name,
            status="closed",
            failures=0,
            changed=was_recovering,
        )

    def reset(self, provider: str | None = None) -> None:
        if provider is None:
            self._states.clear()
            return
        self._states.pop(_provider_key(provider), None)


def _provider_key(provider: str) -> str:
    return (provider or "unknown").strip().lower() or "unknown"
