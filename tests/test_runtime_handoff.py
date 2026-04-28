from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.capability_router import classify_autonomy_intent, route_request
from claw_v2.execution_environment import detect_execution_environment
from claw_v2.runtime_handoff import (
    create_runtime_handoff,
    format_handoff_message,
)


class ExecutionEnvironmentTests(unittest.TestCase):
    def test_claude_code_marker_detected(self) -> None:
        with patch.dict(os.environ, {"CLAUDECODE": "1"}, clear=False):
            env = detect_execution_environment()
            self.assertEqual(env.kind, "claude_code_sandbox")
            self.assertFalse(env.can_run_bash)
            self.assertFalse(env.can_run_python_module)
            self.assertFalse(env.can_access_browser_cli)
            self.assertTrue(env.is_sandboxed)

    def test_claw_production_marker_detected(self) -> None:
        with patch.dict(
            os.environ, {"CLAW_RUNTIME_MODE": "production"}, clear=False
        ):
            env = detect_execution_environment()
            self.assertEqual(env.kind, "claw_production")
            self.assertTrue(env.can_restart_launchd)

    def test_local_terminal_when_no_markers(self) -> None:
        # Strip out any Claude Code or Claw markers before checking
        env_overrides = {
            name: ""
            for name in (
                "CLAUDECODE",
                "CLAUDE_CODE_SESSION",
                "CLAUDE_CODE_PROJECT_DIR",
                "ANTHROPIC_CLAUDE_CODE",
                "CLAW_RUNTIME_MODE",
                "CLAW_DAEMON",
                "LAUNCH_DAEMON_LABEL",
            )
        }
        with patch.dict(os.environ, env_overrides, clear=False):
            env = detect_execution_environment()
            self.assertIn(env.kind, {"local_terminal", "unknown"})


class RuntimeHandoffTests(unittest.TestCase):
    def test_handoff_persists_to_queue_when_gateway_down(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_root = Path(tmpdir) / "runtime_handoffs"
            handoff = create_runtime_handoff(
                goal="ai_news_brief",
                session_id="s1",
                required_capabilities=["web_search"],
                queue_root=queue_root,
                gateway_port=1,  # nothing listening
            )
            self.assertEqual(handoff.dispatch_method, "queue")
            self.assertEqual(handoff.status, "pending_dispatch")
            self.assertTrue(handoff.queue_path)
            persisted = Path(handoff.queue_path)
            self.assertTrue(persisted.exists())
            data = json.loads(persisted.read_text())
            self.assertEqual(data["goal"], "ai_news_brief")
            self.assertEqual(data["session_id"], "s1")

    def test_handoff_message_when_queued_includes_restart_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            handoff = create_runtime_handoff(
                goal="x",
                session_id="s1",
                queue_root=Path(tmpdir),
                gateway_port=1,
            )
            message = format_handoff_message(handoff)
            self.assertIn("./scripts/restart.sh", message)
            self.assertIn("/status", message)

    def test_handoff_uses_http_when_gateway_alive(self) -> None:
        # Spin up a tiny local listener for the duration of the test.
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen(1)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                handoff = create_runtime_handoff(
                    goal="x",
                    session_id="s1",
                    queue_root=Path(tmpdir),
                    gateway_port=port,
                )
                self.assertEqual(handoff.dispatch_method, "http")
                self.assertEqual(handoff.status, "dispatched")
        finally:
            sock.close()


class RouterEnvironmentIntegrationTests(unittest.TestCase):
    def test_sandbox_forces_handoff_for_ai_news(self) -> None:
        intent = classify_autonomy_intent("dame noticias AI de hoy")
        route = route_request(
            intent,
            skill_available=lambda name: True,
            runtime_alive=True,
            current_environment="claude_code_sandbox",
        )
        self.assertEqual(route.route, "runtime_handoff")
        self.assertEqual(route.reason, "claude_code_sandbox_cannot_execute")

    def test_sandbox_forces_handoff_for_x_trends(self) -> None:
        intent = classify_autonomy_intent("X trends ahora")
        route = route_request(
            intent,
            chrome_cdp=True,
            current_environment="claude_code_sandbox",
        )
        self.assertEqual(route.route, "runtime_handoff")

    def test_production_environment_allows_local_routes(self) -> None:
        intent = classify_autonomy_intent("dame noticias AI de hoy")
        route = route_request(
            intent,
            skill_available=lambda name: True,
            runtime_alive=True,
            current_environment="claw_production",
        )
        self.assertEqual(route.route, "skill")

    def test_local_terminal_allows_local_routes(self) -> None:
        intent = classify_autonomy_intent("dame noticias AI de hoy")
        route = route_request(
            intent,
            skill_available=lambda name: True,
            runtime_alive=True,
            current_environment="local_terminal",
        )
        self.assertEqual(route.route, "skill")

    def test_unknown_environment_does_not_force_handoff(self) -> None:
        intent = classify_autonomy_intent("dame noticias AI de hoy")
        route = route_request(
            intent,
            skill_available=lambda name: True,
            runtime_alive=True,
            current_environment="unknown",
        )
        self.assertEqual(route.route, "skill")


if __name__ == "__main__":
    unittest.main()
