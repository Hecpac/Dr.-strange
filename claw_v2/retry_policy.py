from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

RetryAction = Literal["retry", "switch_tool", "ask_user"]


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
