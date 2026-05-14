from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claw_v2.observation_window import (
    ObservationWindowBlocked,
    ObservationWindowConfig,
    ObservationWindowState,
    _diagnostic_only_freeze_reason,
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
            alerts: list[str] = []
            window = ObservationWindowState(
                observe=observe,
                state_path=Path(tmpdir) / "window.json",
            )
            window.set_alert_notifier(alerts.append)
            window.freeze(reason="manual_test", actor="test")

            with self.assertRaises(ObservationWindowBlocked):
                window.before_tool_execution(tool_name="Read", args={}, tier=1, actor="operator")

            window.unfreeze(actor="test")
            window.before_tool_execution(tool_name="Read", args={}, tier=1, actor="operator")

            event_names = [name for name, _ in observe.events]
            self.assertIn("observation_window_freeze_set", event_names)
            self.assertIn("tool_blocked_by_freeze", event_names)
            self.assertIn("observation_window_freeze_cleared", event_names)
            self.assertIn("Observation window frozen: manual_test", alerts)

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
            alerts: list[str] = []
            diagnostic_stream: list[str] = []
            window = ObservationWindowState(
                observe=observe,
                state_path=Path(tmpdir) / "window.json",
                config=ObservationWindowConfig(cost_per_hour_threshold=0.05),
            )
            window.set_alert_notifier(alerts.append)
            window.set_stream_notifier(diagnostic_stream.append)

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
            # cost_per_hour is a budget alarm — operator must be notified.
            self.assertTrue(
                any("circuit_breaker:cost_per_hour" in alert for alert in alerts),
                f"expected cost_per_hour alert to reach notifier, got: {alerts!r}",
            )
            self.assertTrue(any("circuit_breaker=cost_per_hour" in line for line in diagnostic_stream))

    def test_cost_per_hour_is_not_diagnostic_only(self) -> None:
        # Regression: budget breaker must escape the diagnostic-silence path so it
        # reaches Telegram. Other circuit_breaker:* reasons stay silent.
        self.assertFalse(_diagnostic_only_freeze_reason("circuit_breaker:cost_per_hour"))
        self.assertTrue(_diagnostic_only_freeze_reason("circuit_breaker:tool_calls_per_minute"))
        self.assertTrue(_diagnostic_only_freeze_reason("circuit_breaker:provider_failure"))

    def test_stale_circuit_breaker_freeze_auto_clears_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "window.json"
            stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
            state_path.write_text(
                json.dumps(
                    {
                        "frozen": True,
                        "reason": "circuit_breaker:cost_per_hour",
                        "actor": "brain",
                        "updated_at": stale_ts,
                    }
                ),
                encoding="utf-8",
            )
            observe = _RecordingObserve()
            window = ObservationWindowState(
                observe=observe,
                state_path=state_path,
                config=ObservationWindowConfig(stale_freeze_seconds=60.0),
            )
            self.assertFalse(window.frozen)
            self.assertEqual(window.freeze_reason, "")
            event_names = [name for name, _ in observe.events]
            self.assertIn("observation_window_freeze_auto_cleared", event_names)
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertFalse(persisted["frozen"])
            self.assertEqual(persisted["actor"], "auto_clear_stale")

    def test_recent_circuit_breaker_freeze_persists_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "window.json"
            recent_ts = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
            state_path.write_text(
                json.dumps(
                    {
                        "frozen": True,
                        "reason": "circuit_breaker:cost_per_hour",
                        "actor": "brain",
                        "updated_at": recent_ts,
                    }
                ),
                encoding="utf-8",
            )
            window = ObservationWindowState(
                state_path=state_path,
                config=ObservationWindowConfig(stale_freeze_seconds=60.0),
            )
            self.assertTrue(window.frozen)
            self.assertEqual(window.freeze_reason, "circuit_breaker:cost_per_hour")

    def test_manual_freeze_does_not_auto_clear_even_when_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "window.json"
            stale_ts = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            state_path.write_text(
                json.dumps(
                    {
                        "frozen": True,
                        "reason": "manual_telegram",
                        "actor": "telegram:1",
                        "updated_at": stale_ts,
                    }
                ),
                encoding="utf-8",
            )
            window = ObservationWindowState(
                state_path=state_path,
                config=ObservationWindowConfig(stale_freeze_seconds=60.0),
            )
            self.assertTrue(window.frozen)
            self.assertEqual(window.freeze_reason, "manual_telegram")

    def test_critical_action_alert_can_notify_with_safe_brief_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = _RecordingObserve()
            alerts: list[str] = []
            window = ObservationWindowState(
                observe=observe,
                state_path=Path(tmpdir) / "window.json",
            )
            window.set_alert_notifier(alerts.append)

            with self.assertRaises(ObservationWindowBlocked):
                window.before_tool_execution(
                    tool_name="Bash",
                    args={"command": "git push --force origin main"},
                    tier=3,
                    actor="operator",
                )

            self.assertEqual(len(alerts), 1)
            self.assertIn("Blocked hard-denylisted tool", alerts[0])
            self.assertNotIn("Circuit breaker tripped", alerts[0])
            self.assertTrue(any(name == "tool_hard_denylist_blocked" for name, _ in observe.events))


if __name__ == "__main__":
    unittest.main()
