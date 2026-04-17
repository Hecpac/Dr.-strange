from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.ecosystem import EcosystemHealthService, EcosystemHealth, EcosystemMetric


class CollectTests(unittest.TestCase):
    def test_returns_ok_when_all_healthy(self) -> None:
        bus = MagicMock()
        bus.pending_count.return_value = 0
        observe = MagicMock()
        observe.cost_per_agent_today.return_value = {"hex": 0.1}
        heartbeat = MagicMock()
        heartbeat.collect.return_value = MagicMock(agents={"hex": {"paused": False}})
        svc = EcosystemHealthService(bus=bus, observe=observe, dream_states={}, heartbeat=heartbeat)
        health = svc.collect()
        self.assertEqual(health.overall, "OK")
        self.assertGreater(len(health.metrics), 0)

    def test_bus_lag_warns_on_many_pending(self) -> None:
        bus = MagicMock()
        bus.pending_count.side_effect = lambda name: 5
        observe = MagicMock()
        observe.cost_per_agent_today.return_value = {}
        heartbeat = MagicMock()
        heartbeat.collect.return_value = MagicMock(agents={})
        svc = EcosystemHealthService(bus=bus, observe=observe, dream_states={}, heartbeat=heartbeat)
        health = svc.collect()
        bus_metric = next(m for m in health.metrics if m.name == "bus_lag")
        self.assertEqual(bus_metric.status, "CRITICAL")  # 5*4=20 > 10
        self.assertEqual(health.overall, "CRITICAL")

    def test_bus_lag_ok_when_empty(self) -> None:
        bus = MagicMock()
        bus.pending_count.return_value = 0
        observe = MagicMock()
        observe.cost_per_agent_today.return_value = {}
        heartbeat = MagicMock()
        svc = EcosystemHealthService(bus=bus, observe=observe, dream_states={}, heartbeat=heartbeat)
        health = svc.collect()
        bus_metric = next(m for m in health.metrics if m.name == "bus_lag")
        self.assertEqual(bus_metric.status, "OK")


class DashboardTests(unittest.TestCase):
    def test_writes_markdown_file(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        dashboard = tmpdir / "ecosystem-health.md"
        bus = MagicMock()
        bus.pending_count.return_value = 0
        observe = MagicMock()
        observe.cost_per_agent_today.return_value = {}
        heartbeat = MagicMock()
        heartbeat.collect.return_value = MagicMock(agents={})
        svc = EcosystemHealthService(bus=bus, observe=observe, dream_states={}, heartbeat=heartbeat)
        svc.write_dashboard(dashboard)
        self.assertTrue(dashboard.exists())
        content = dashboard.read_text()
        self.assertIn("Ecosystem Health", content)
        self.assertIn("Overall", content)
        self.assertIn("bus_lag", content)
