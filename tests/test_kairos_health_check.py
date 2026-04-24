from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.kairos import KairosService, TickDecision


def _make_service():
    router = MagicMock()
    heartbeat = MagicMock()
    observe = MagicMock()
    heartbeat.collect.return_value = MagicMock(
        pending_approvals=0,
        pending_approval_ids=[],
        agents={},
        lane_metrics={},
    )
    svc = KairosService(
        router=router,
        heartbeat=heartbeat,
        observe=observe,
        action_budget=15.0,
        brief=True,
    )
    return svc, observe


class DaemonHealthCheckHandlerTests(unittest.TestCase):
    def test_ok_path_emits_notification_with_ok_status(self) -> None:
        svc, observe = _make_service()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "claw.log"
            log_path.write_text("2026-04-24 INFO all good\nheartbeat tick ok\n")
            fake_pgrep = subprocess.CompletedProcess(
                args=["pgrep"], returncode=0, stdout="12345 python claw_v2\n", stderr=""
            )
            with patch("claw_v2.kairos.subprocess.run", return_value=fake_pgrep), \
                 patch("claw_v2.kairos.Path") as mock_path:
                mock_path.return_value = log_path
                svc._handle_daemon_health_check(TickDecision(action="daemon_health_check"), None)

        observe.emit.assert_called_once()
        args, kwargs = observe.emit.call_args
        self.assertEqual(args[0], "daemon_health_check_notification")
        payload = kwargs["payload"]
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["pids"], ["12345"])
        self.assertEqual(payload["anomaly_tokens_found"], [])
        self.assertEqual(payload["auto_approved_reason"], "Scheduled Daemon Health Check")
        self.assertIsNone(payload["error"])

    def test_anomaly_path_detects_traceback(self) -> None:
        svc, observe = _make_service()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "claw.log"
            log_path.write_text("ok\nTraceback (most recent call last):\nSomeError: boom\n")
            fake_pgrep = subprocess.CompletedProcess(
                args=["pgrep"], returncode=0, stdout="42 python claw_v2\n", stderr=""
            )
            with patch("claw_v2.kairos.subprocess.run", return_value=fake_pgrep), \
                 patch("claw_v2.kairos.Path") as mock_path:
                mock_path.return_value = log_path
                svc._handle_daemon_health_check(TickDecision(action="daemon_health_check"), None)

        args, kwargs = observe.emit.call_args
        payload = kwargs["payload"]
        self.assertEqual(payload["status"], "anomaly")
        self.assertIn("Traceback", payload["anomaly_tokens_found"])

    def test_pgrep_failure_is_fail_safe(self) -> None:
        svc, observe = _make_service()
        with patch(
            "claw_v2.kairos.subprocess.run",
            side_effect=FileNotFoundError("pgrep not found"),
        ):
            svc._handle_daemon_health_check(TickDecision(action="daemon_health_check"), None)

        args, kwargs = observe.emit.call_args
        payload = kwargs["payload"]
        self.assertEqual(payload["status"], "check_failed")
        self.assertIn("pgrep_failed", payload["error"])
        self.assertEqual(payload["pids"], [])

    def test_missing_log_file_is_ok_not_anomaly(self) -> None:
        svc, observe = _make_service()
        fake_pgrep = subprocess.CompletedProcess(
            args=["pgrep"], returncode=0, stdout="1 claw_v2\n", stderr=""
        )
        missing = Path(tempfile.gettempdir()) / "definitely_not_a_log_xyz.log"
        if missing.exists():
            missing.unlink()
        with patch("claw_v2.kairos.subprocess.run", return_value=fake_pgrep), \
             patch("claw_v2.kairos.Path") as mock_path:
            mock_path.return_value = missing
            svc._handle_daemon_health_check(TickDecision(action="daemon_health_check"), None)

        args, kwargs = observe.emit.call_args
        payload = kwargs["payload"]
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["log_tail_count"], 0)

    def test_payload_contains_auto_approved_reason(self) -> None:
        svc, observe = _make_service()
        with patch(
            "claw_v2.kairos.subprocess.run",
            side_effect=RuntimeError("boom"),
        ):
            svc._handle_daemon_health_check(TickDecision(action="daemon_health_check"), None)
        _, kwargs = observe.emit.call_args
        self.assertEqual(
            kwargs["payload"]["auto_approved_reason"], "Scheduled Daemon Health Check"
        )

    def test_emit_failure_does_not_crash_handler(self) -> None:
        svc, observe = _make_service()
        observe.emit.side_effect = RuntimeError("db locked")
        with patch(
            "claw_v2.kairos.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ):
            svc._handle_daemon_health_check(TickDecision(action="daemon_health_check"), None)

    def test_run_health_check_dispatches_event(self) -> None:
        svc, _ = _make_service()
        with patch.object(svc, "handle_event", return_value=TickDecision(action="daemon_health_check")) as mock_he:
            result = svc.run_health_check()
        mock_he.assert_called_once_with("daemon_health_check", payload={})
        self.assertEqual(result.action, "daemon_health_check")


class DaemonHealthRegisteredHandlerTests(unittest.TestCase):
    def test_action_registered_in_handlers_map(self) -> None:
        svc, _ = _make_service()
        decision = TickDecision(action="daemon_health_check")
        with patch.object(svc, "_handle_daemon_health_check") as mock_handler:
            svc._execute(decision, budget=5.0, trace_context=None)
        mock_handler.assert_called_once()


if __name__ == "__main__":
    unittest.main()
