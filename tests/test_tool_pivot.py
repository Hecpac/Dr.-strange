"""Tool pivoting tests — Wave 2.1.

`ToolRegistry.execute_with_pivot` lets a caller declare alternative tools to
try when the primary is blocked by a tool-specific failure (sandbox path
violation, hard denylist match, agent-class mismatch). Systemic blocks
(observation window frozen, rate-limit breaker) abort immediately because
retrying with another tool would hit the same window.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.observation_window import ObservationWindowBlocked
from claw_v2.tools import (
    TIER_READ_ONLY,
    ToolDefinition,
    ToolRegistry,
)


class _RecordingObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **kwargs: object) -> None:
        self.events.append((event_type, dict(kwargs)))


def _build_registry(tmpdir: Path, observe: _RecordingObserve | None = None) -> ToolRegistry:
    workspace = tmpdir / "workspace"
    workspace.mkdir(exist_ok=True)
    return ToolRegistry(workspace_root=workspace, observe=observe)


def _tool(name: str, handler) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"test tool {name}",
        allowed_agent_classes=("operator", "researcher"),
        handler=handler,
        tier=TIER_READ_ONLY,
    )


class ToolPivotTests(unittest.TestCase):
    def test_execute_with_pivot_falls_back_to_alternative_when_primary_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = _RecordingObserve()
            registry = _build_registry(Path(tmpdir), observe=observe)
            registry.register(_tool("BlockedA", lambda args: (_ for _ in ()).throw(PermissionError("sandbox: path /etc/x denied"))))
            registry.register(_tool("WorkingB", lambda args: {"ok": True, "from": "B"}))

            result = registry.execute_with_pivot(
                "BlockedA", {"x": 1}, agent_class="operator", alternatives=["WorkingB"]
            )

            self.assertEqual(result["from"], "B")
            pivot_events = [event for name, event in observe.events if name == "tool_pivot"]
            self.assertEqual(len(pivot_events), 1)
            self.assertEqual(pivot_events[0]["payload"]["from_tool"], "BlockedA")
            self.assertEqual(pivot_events[0]["payload"]["to_tool"], "WorkingB")
            self.assertIn("PermissionError", pivot_events[0]["payload"]["reason"])

    def test_execute_with_pivot_raises_when_all_alternatives_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = _RecordingObserve()
            registry = _build_registry(Path(tmpdir), observe=observe)
            registry.register(_tool("BlockedA", lambda args: (_ for _ in ()).throw(PermissionError("a"))))
            registry.register(_tool("BlockedB", lambda args: (_ for _ in ()).throw(PermissionError("b"))))

            with self.assertRaises(PermissionError):
                registry.execute_with_pivot(
                    "BlockedA", {}, agent_class="operator", alternatives=["BlockedB"]
                )

            pivot_events = [event for name, event in observe.events if name == "tool_pivot"]
            # Only one pivot (A → B); B failure has no further alternative.
            self.assertEqual(len(pivot_events), 1)

    def test_execute_with_pivot_does_not_pivot_on_systemic_observation_window_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = _RecordingObserve()
            registry = _build_registry(Path(tmpdir), observe=observe)

            def systemic_block(args):
                raise ObservationWindowBlocked("observation window frozen: circuit_breaker:cost_per_hour")

            registry.register(_tool("BlockedSystemic", systemic_block))
            registry.register(_tool("Alt", lambda args: {"ok": True}))

            with self.assertRaises(ObservationWindowBlocked):
                registry.execute_with_pivot(
                    "BlockedSystemic", {}, agent_class="operator", alternatives=["Alt"]
                )

            pivot_events = [event for name, event in observe.events if name == "tool_pivot"]
            self.assertEqual(
                pivot_events,
                [],
                "Systemic blocks (frozen circuit) must not pivot — same window blocks every alternative.",
            )

    def test_execute_with_pivot_does_not_pivot_on_tool_calls_per_minute_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = _RecordingObserve()
            registry = _build_registry(Path(tmpdir), observe=observe)

            def rate_limit_block(args):
                raise ObservationWindowBlocked("tool_calls_per_minute breaker tripped: 11 > 10")

            registry.register(_tool("RateLimited", rate_limit_block))
            registry.register(_tool("Alt", lambda args: {"ok": True}))

            with self.assertRaises(ObservationWindowBlocked):
                registry.execute_with_pivot(
                    "RateLimited", {}, agent_class="operator", alternatives=["Alt"]
                )

            self.assertFalse(any(name == "tool_pivot" for name, _ in observe.events))

    def test_execute_with_pivot_picks_up_policy_fallback_tools_when_no_explicit_alternatives(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = _RecordingObserve()
            registry = _build_registry(Path(tmpdir), observe=observe)
            registry.register(_tool("BlockedA", lambda args: (_ for _ in ()).throw(PermissionError("blocked"))))
            registry.register(_tool("PolicyFallback", lambda args: {"ok": True, "via": "policy"}))

            from claw_v2.tool_policy import TOOL_POLICIES, ToolPolicy

            TOOL_POLICIES["BlockedA"] = ToolPolicy(
                name="BlockedA",
                risk_level="low",
                read_only=True,
                allowed_contexts=frozenset({"operator"}),
                fallback_tools=("PolicyFallback",),
            )
            try:
                result = registry.execute_with_pivot(
                    "BlockedA", {}, agent_class="operator"
                )
                self.assertEqual(result["via"], "policy")
            finally:
                del TOOL_POLICIES["BlockedA"]

    def test_explicit_alternatives_override_policy_fallback_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = _RecordingObserve()
            registry = _build_registry(Path(tmpdir), observe=observe)
            registry.register(_tool("BlockedA", lambda args: (_ for _ in ()).throw(PermissionError("blocked"))))
            registry.register(_tool("ExplicitB", lambda args: {"ok": True, "via": "explicit"}))
            registry.register(_tool("PolicyC", lambda args: {"ok": True, "via": "policy"}))

            from claw_v2.tool_policy import TOOL_POLICIES, ToolPolicy

            TOOL_POLICIES["BlockedA"] = ToolPolicy(
                name="BlockedA",
                risk_level="low",
                read_only=True,
                allowed_contexts=frozenset({"operator"}),
                fallback_tools=("PolicyC",),
            )
            try:
                result = registry.execute_with_pivot(
                    "BlockedA", {}, agent_class="operator", alternatives=["ExplicitB"]
                )
                self.assertEqual(result["via"], "explicit")
            finally:
                del TOOL_POLICIES["BlockedA"]


if __name__ == "__main__":
    unittest.main()
