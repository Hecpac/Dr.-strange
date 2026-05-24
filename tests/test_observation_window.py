from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claw_v2.observation_window import (
    LOCAL_READ_ONLY_TIER,
    ObservationWindowBlocked,
    ObservationWindowConfig,
    ObservationWindowState,
    _diagnostic_only_freeze_reason,
    _is_cost_breaker_reason,
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

    def test_notional_subscription_cost_does_not_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = _RecordingObserve()
            window = ObservationWindowState(
                observe=observe,
                state_path=Path(tmpdir) / "window.json",
                config=ObservationWindowConfig(
                    cost_per_hour_threshold=0.05,
                    notional_cost_providers=("anthropic", "codex"),
                ),
            )

            window.handle_llm_audit_event(
                {
                    "action": "llm_response",
                    "lane": "brain",
                    "provider": "anthropic",
                    "model": "claude-opus-4-7",
                    "cost_estimate": 9.99,
                    "degraded_mode": False,
                    "metadata": {},
                }
            )

            self.assertFalse(window.frozen)
            event_names = [name for name, _ in observe.events]
            self.assertIn("llm_notional_cost_ignored", event_names)
            self.assertNotIn("circuit_breaker_tripped", event_names)

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


class CostBreakerTierSplitTests(unittest.TestCase):
    """PR 0A: LLM cost-per-hour breaker must not block Tier-1 local read tools."""

    def _make_window(
        self,
        tmpdir: str,
        *,
        cost_threshold: float = 0.05,
        rate_threshold: int = 1000,
    ) -> tuple[ObservationWindowState, _RecordingObserve]:
        observe = _RecordingObserve()
        window = ObservationWindowState(
            observe=observe,
            state_path=Path(tmpdir) / "window.json",
            config=ObservationWindowConfig(
                cost_per_hour_threshold=cost_threshold,
                tool_calls_per_minute_threshold=rate_threshold,
            ),
        )
        return window, observe

    def _trip_cost_breaker(self, window: ObservationWindowState) -> None:
        window.handle_llm_audit_event(
            {
                "action": "llm_response",
                "lane": "brain",
                "provider": "anthropic",
                "model": "claude",
                "cost_estimate": 1.00,
                "degraded_mode": False,
                "metadata": {},
            }
        )
        self.assertTrue(window.frozen)
        self.assertEqual(window.freeze_reason, "circuit_breaker:cost_per_hour")

    def test_cost_breaker_does_not_block_tier1_read_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            window, observe = self._make_window(tmpdir)
            self._trip_cost_breaker(window)
            for tool in ("Read", "Grep", "Glob"):
                with self.subTest(tool=tool):
                    window.before_tool_execution(
                        tool_name=tool,
                        args={},
                        tier=LOCAL_READ_ONLY_TIER,
                        actor="operator",
                    )
            allowed_events = [
                payload for name, payload in observe.events
                if name == "tool_allowed_during_cost_breaker"
            ]
            self.assertEqual(len(allowed_events), 3)
            self.assertEqual(allowed_events[0]["payload"]["freeze_reason"], "circuit_breaker:cost_per_hour")
            blocked_events = [
                payload for name, payload in observe.events
                if name == "tool_blocked_by_freeze"
            ]
            self.assertEqual(blocked_events, [])

    def test_cost_breaker_blocks_or_degrades_llm_calls(self) -> None:
        # Observation window's job: emit autonomy_degraded_by_cost_breaker
        # AND keep the freeze set so downstream LLM router sees it.
        with tempfile.TemporaryDirectory() as tmpdir:
            window, observe = self._make_window(tmpdir)
            self._trip_cost_breaker(window)
            degraded = [
                payload for name, payload in observe.events
                if name == "autonomy_degraded_by_cost_breaker"
            ]
            self.assertEqual(len(degraded), 1)
            self.assertIn("llm_calls_until_window_decays", degraded[0]["payload"]["blocked_capabilities"])
            self.assertTrue(window.frozen)
            self.assertEqual(window.freeze_reason, "circuit_breaker:cost_per_hour")

    def test_hard_denylist_still_blocks_tools_even_when_tier1(self) -> None:
        # Hard denylist must fire regardless of tier or freeze state.
        with tempfile.TemporaryDirectory() as tmpdir:
            window, observe = self._make_window(tmpdir)
            self._trip_cost_breaker(window)
            with self.assertRaises(ObservationWindowBlocked):
                window.before_tool_execution(
                    tool_name="Bash",
                    args={"command": "git push --force origin main"},
                    tier=LOCAL_READ_ONLY_TIER,
                    actor="operator",
                )
            denylist_events = [
                payload for name, payload in observe.events
                if name == "tool_hard_denylist_blocked"
            ]
            self.assertEqual(len(denylist_events), 1)

    def test_tool_rate_breaker_still_blocks_excessive_tools(self) -> None:
        # Tool-call-rate breaker is independent of cost breaker and must still trip.
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = _RecordingObserve()
            now = [1000.0]
            window = ObservationWindowState(
                observe=observe,
                state_path=Path(tmpdir) / "window.json",
                config=ObservationWindowConfig(
                    cost_per_hour_threshold=10.00,
                    tool_calls_per_minute_threshold=2,
                ),
                clock=lambda: now[0],
            )
            window.before_tool_execution(tool_name="Read", args={}, tier=1, actor="operator")
            window.before_tool_execution(tool_name="Read", args={}, tier=1, actor="operator")
            with self.assertRaises(ObservationWindowBlocked):
                window.before_tool_execution(tool_name="Read", args={}, tier=1, actor="operator")
            self.assertEqual(window.freeze_reason, "circuit_breaker:tool_calls_per_minute")
            # Tool-rate freeze is NOT a cost breaker — subsequent Tier-1 reads stay blocked.
            with self.assertRaises(ObservationWindowBlocked):
                window.before_tool_execution(tool_name="Grep", args={}, tier=1, actor="operator")

    def test_external_or_tier3_tools_not_auto_allowed_by_cost_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            window, observe = self._make_window(tmpdir)
            self._trip_cost_breaker(window)
            for tier in (2, 3):
                with self.subTest(tier=tier):
                    with self.assertRaises(ObservationWindowBlocked):
                        window.before_tool_execution(
                            tool_name="WriteOrDeploy",
                            args={},
                            tier=tier,
                            actor="operator",
                        )

    def test_autonomy_degraded_event_emitted_when_cost_breaker_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            window, observe = self._make_window(tmpdir)
            self._trip_cost_breaker(window)
            degraded = [
                (name, payload) for name, payload in observe.events
                if name == "autonomy_degraded_by_cost_breaker"
            ]
            self.assertEqual(len(degraded), 1)
            payload = degraded[0][1]["payload"]
            self.assertEqual(payload["actor"], "brain")
            self.assertEqual(payload["lane"], "brain")
            self.assertEqual(payload["provider"], "anthropic")
            self.assertIn("tier_1_local_read_only", payload["allowed_capabilities"])
            self.assertGreater(payload["value"], payload["threshold"])

    def test_no_manual_handoff_required_when_tier1_tools_are_available(self) -> None:
        # Behavioral contract: while cost breaker is open, the bot can still
        # call safe local read tools, so it has no operational reason to
        # respond with "ejecuta este comando" / manual handoff for inspection.
        with tempfile.TemporaryDirectory() as tmpdir:
            window, _ = self._make_window(tmpdir)
            self._trip_cost_breaker(window)
            # Each of these would otherwise force a manual-handoff fallback.
            window.before_tool_execution(tool_name="Read", args={}, tier=1, actor="brain")
            window.before_tool_execution(tool_name="Grep", args={}, tier=1, actor="brain")
            window.before_tool_execution(tool_name="Glob", args={}, tier=1, actor="brain")
            # WikiSearch is local/read-only by policy.
            window.before_tool_execution(tool_name="WikiSearch", args={}, tier=1, actor="brain")

    def test_is_cost_breaker_reason_helper(self) -> None:
        self.assertTrue(_is_cost_breaker_reason("circuit_breaker:cost_per_hour"))
        self.assertFalse(_is_cost_breaker_reason("circuit_breaker:tool_calls_per_minute"))
        self.assertFalse(_is_cost_breaker_reason("manual_telegram"))
        self.assertFalse(_is_cost_breaker_reason(""))


if __name__ == "__main__":
    unittest.main()
