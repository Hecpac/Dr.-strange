from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.observation_window import (
    ObservationWindowBlocked,
    ObservationWindowConfig,
    ObservationWindowState,
    hard_denylist_reason,
)


class _RecordingObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **kwargs: object) -> None:
        self.events.append((event, dict(kwargs)))


class ObservationWindowTests(unittest.TestCase):
    def test_hard_denylist_matches_blocked_shell_commands(self) -> None:
        cases = [
            "git push --force origin main",
            "git -C repo push -f origin main",
            "vercel --prod",
            "gh release create v1.0.0",
            "rm -rf $TARGET",
            "rm -rf build/*",
        ]
        for command in cases:
            with self.subTest(command=command):
                self.assertIsNotNone(hard_denylist_reason("Bash", {"command": command}))

    def test_freeze_blocks_tool_execution_and_unfreeze_reopens(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = _RecordingObserve()
            window = ObservationWindowState(
                observe=observe,
                state_path=Path(tmpdir) / "window.json",
            )
            window.freeze(reason="manual_test", actor="test")

            with self.assertRaises(ObservationWindowBlocked):
                window.before_tool_execution(tool_name="Read", args={}, tier=1, actor="operator")

            window.unfreeze(actor="test")
            window.before_tool_execution(tool_name="Read", args={}, tier=1, actor="operator")

            event_names = [name for name, _ in observe.events]
            self.assertIn("observation_window_freeze_set", event_names)
            self.assertIn("tool_blocked_by_freeze", event_names)
            self.assertIn("observation_window_freeze_cleared", event_names)

    def test_tool_calls_per_minute_breaker_freezes_and_blocks_current_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = _RecordingObserve()
            now = [1000.0]
            window = ObservationWindowState(
                observe=observe,
                state_path=Path(tmpdir) / "window.json",
                config=ObservationWindowConfig(tool_calls_per_minute_threshold=2),
                clock=lambda: now[0],
            )

            window.before_tool_execution(tool_name="Read", args={}, tier=1, actor="operator")
            window.before_tool_execution(tool_name="Read", args={}, tier=1, actor="operator")
            with self.assertRaises(ObservationWindowBlocked):
                window.before_tool_execution(tool_name="Read", args={}, tier=1, actor="operator")

            self.assertTrue(window.frozen)
            self.assertTrue(any(name == "circuit_breaker_tripped" for name, _ in observe.events))

    def test_llm_cost_per_hour_breaker_freezes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = _RecordingObserve()
            window = ObservationWindowState(
                observe=observe,
                state_path=Path(tmpdir) / "window.json",
                config=ObservationWindowConfig(cost_per_hour_threshold=0.05),
            )

            window.handle_llm_audit_event(
                {
                    "action": "llm_response",
                    "lane": "brain",
                    "provider": "anthropic",
                    "model": "claude",
                    "cost_estimate": 0.06,
                    "degraded_mode": False,
                    "metadata": {},
                }
            )

            self.assertTrue(window.frozen)
            breaker_events = [payload for name, payload in observe.events if name == "circuit_breaker_tripped"]
            self.assertEqual(breaker_events[0]["payload"]["breaker"], "cost_per_hour")


if __name__ == "__main__":
    unittest.main()
