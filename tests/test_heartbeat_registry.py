from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.heartbeat import HeartbeatService, _compute_health, update_agent_registry


class ComputeHealthTests(unittest.TestCase):
    def test_ok_when_active_no_errors(self) -> None:
        info = {"paused": False, "cost_today": 1.0, "daily_budget": 10.0}
        self.assertEqual(_compute_health(info), "OK")

    def test_warn_budget(self) -> None:
        info = {"paused": False, "cost_today": 9.0, "daily_budget": 10.0}
        self.assertEqual(_compute_health(info), "WARN:budget")

    def test_critical_when_paused(self) -> None:
        info = {"paused": True}
        self.assertEqual(_compute_health(info), "CRITICAL")

    def test_warn_errors(self) -> None:
        info = {"paused": False, "has_errors": True, "cost_today": 0, "daily_budget": 10.0}
        self.assertEqual(_compute_health(info), "WARN:errors")


class RegistryWriteTests(unittest.TestCase):
    def test_emit_writes_agents_md(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        registry_path = tmpdir / "AGENTS.md"
        metrics = MagicMock()
        metrics.snapshot.return_value = {}
        approvals = MagicMock()
        approvals.list_pending.return_value = []
        agent_store = MagicMock()
        agent_store.list_agents.return_value = ["hex"]
        agent_store.load_state.return_value = {
            "agent_class": "operator",
            "paused": False,
            "last_verified_state": {"metric": 0.95},
        }
        observe = MagicMock()
        svc = HeartbeatService(
            metrics=metrics,
            approvals=approvals,
            agent_store=agent_store,
            observe=observe,
            registry_path=registry_path,
        )
        svc.emit()
        self.assertTrue(registry_path.exists())
        content = registry_path.read_text()
        self.assertIn("hex", content)
        self.assertIn("Agent", content)
        observe.emit.assert_any_call("agent_registry_updated")

    def test_emit_without_registry_path_skips_write(self) -> None:
        metrics = MagicMock()
        metrics.snapshot.return_value = {}
        approvals = MagicMock()
        approvals.list_pending.return_value = []
        agent_store = MagicMock()
        agent_store.list_agents.return_value = []
        observe = MagicMock()
        svc = HeartbeatService(
            metrics=metrics,
            approvals=approvals,
            agent_store=agent_store,
            observe=observe,
        )
        snapshot = svc.emit()
        self.assertIsNotNone(snapshot)
