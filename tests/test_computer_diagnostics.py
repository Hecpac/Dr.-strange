from __future__ import annotations

import asyncio
import base64
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from claw_v2 import liveness
from claw_v2.computer import ComputerSession
from claw_v2.computer_handler import ComputerHandler
from claw_v2.diagnostics import collect_diagnostics
from claw_v2.main import _run_startup_healthchecks
from claw_v2.observe import ObserveStream
from tests.helpers import make_config


class _UrlopenResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_UrlopenResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self._body


def _completed(
    args: list[str], returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=args, returncode=returncode, stdout=stdout, stderr=stderr
    )


def _healthy_runner(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
    if args[:2] == ["launchctl", "list"]:
        return _completed(args, 0, "123\t0\tcom.pachano.claw\n")
    if args[:2] == ["launchctl", "print"]:
        return _completed(args, 0, "state = running\n")
    if args[:2] == ["pgrep", "-fl"]:
        return _completed(args, 0, "123 .venv/bin/python -m claw_v2.main\n")
    if args and args[0] == "lsof":
        return _completed(args, 0, "Python 123 user 3u IPv4 TCP 127.0.0.1:8765 (LISTEN)\n")
    return _completed(args, 1, "", "unknown command")


def _seed_current_daemon_window(observe: ObserveStream, db_path: Path) -> None:
    observe.emit(
        "agent_startup_context",
        payload={"pid": 123, "boot_id": "boot-current", "code_version": "test-code"},
    )
    liveness.write_liveness(
        liveness.liveness_sink_path(db_path.parent),
        {
            "pid": 123,
            "boot_id": "boot-current",
            "ts": time.time(),
            "web_transport_serving": True,
            "source": "test",
        },
    )


class ComputerDiagnosticsTests(unittest.TestCase):
    def test_computer_diag_reports_backend_screenshot_and_cdp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "claw.db")
            computer = MagicMock()
            computer.codex_backend = SimpleNamespace(cli_path="codex")
            computer.capture_screenshot.return_value = {
                "data": base64.b64encode(b"png-bytes").decode("ascii"),
                "media_type": "image/png",
            }
            browser_use = SimpleNamespace(cdp_url="http://127.0.0.1:9250")
            cdp_payload = b'{"Browser":"Chrome/148","User-Agent":"Mozilla/5.0 HeadlessChrome/148"}'
            handler = ComputerHandler(
                computer=computer,
                browser_use=browser_use,
                observe=observe,
            )

            with patch(
                "claw_v2.computer_handler.urllib.request.urlopen",
                return_value=_UrlopenResponse(cdp_payload),
            ):
                response = handler.diagnostics_response("s1")

            self.assertIn("Diagnostico Computer Use: ok", response)
            self.assertIn("backend: ok - codex", response)
            self.assertIn("screenshot: ok - image/png", response)
            self.assertIn("browser_use_cdp: ok - Chrome/148; headless=True", response)
            events = observe.recent_events(limit=10)
            result = next(
                event for event in events if event["event_type"] == "computer_diagnostic_result"
            )
            self.assertEqual(result["payload"]["status"], "ok")
            checks = {item["name"]: item for item in result["payload"]["checks"]}
            self.assertEqual(checks["screenshot"]["status"], "ok")
            self.assertEqual(checks["browser_use_cdp"]["status"], "ok")

    def test_browser_use_timeout_emits_structured_diagnostics(self) -> None:
        class TimeoutBrowserUse:
            async def run_task(self, _instruction: str, **kwargs) -> str:
                raise asyncio.TimeoutError()

        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "claw.db")
            handler = ComputerHandler(
                computer=SimpleNamespace(codex_backend=SimpleNamespace(cli_path="codex")),
                browser_use=TimeoutBrowserUse(),
                observe=observe,
            )
            handler._sessions["s1"] = ComputerSession(
                task="Use ChatGPT to create an image",
                current_url="https://chatgpt.com/",
                pending_action={
                    "action": "browser_use_task",
                    "backend": "browser_use",
                    "approved": True,
                    "approval_id": "approval-1",
                },
            )

            response = handler._run_session("s1")

            self.assertIn("browser_use timed out after 180s", response)
            events = observe.recent_events(limit=10)
            event_types = [event["event_type"] for event in events]
            self.assertIn("computer_browser_use_timeout", event_types)
            self.assertIn("computer_session_failed", event_types)
            timeout_event = next(
                event for event in events if event["event_type"] == "computer_browser_use_timeout"
            )
            self.assertEqual(timeout_event["payload"]["backend"], "browser_use")
            self.assertEqual(timeout_event["payload"]["timeout_seconds"], 180)

    def test_startup_health_reports_display_and_missing_openai_key_without_disabling_mocks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = make_config(root)
            config.computer_use_enabled = True
            config.computer_use_backend = "openai"
            config.openai_api_key = None
            observe = ObserveStream(config.db_path)

            with patch("claw_v2.main._probe_pyautogui_display", return_value=(0, 0)):
                report = _run_startup_healthchecks(config, observe)

            degraded = {item.name: item for item in report.degraded}
            self.assertIn("computer_display", degraded)
            self.assertIn("openai_api_key", degraded)
            self.assertIsNone(degraded["openai_api_key"].capability)
            events = observe.recent_events(limit=20)
            degraded_events = [
                event for event in events if event["event_type"] == "startup_healthcheck_degraded"
            ]
            self.assertTrue(
                any(event["payload"]["name"] == "computer_display" for event in degraded_events)
            )
            self.assertTrue(
                any(event["payload"]["name"] == "openai_api_key" for event in degraded_events)
            )

    def test_startup_health_emits_model_role_summary_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = make_config(root)
            config.computer_use_enabled = False
            config.computer_use_backend = "codex"
            config.codex_model = "gpt-5.5"
            config.computer_browser_use_model = "claude-sonnet-4-6"
            observe = ObserveStream(config.db_path)

            report = _run_startup_healthchecks(config, observe)

            model_roles = next(item for item in report.ok if item.name == "model_roles")
            self.assertIn("computer_use_primary=codex:gpt-5.5", model_roles.detail)
            self.assertIn("computer_use_fast=codex:gpt-5.4-mini", model_roles.detail)
            self.assertIn("browser_agent_primary=anthropic:claude-sonnet-4-6", model_roles.detail)
            self.assertIn("browser_agent_fallback=disabled", model_roles.detail)
            self.assertNotIn("KEY", model_roles.detail)
            events = observe.recent_events(limit=50)
            ok_events = [
                event for event in events if event["event_type"] == "startup_healthcheck_ok"
            ]
            self.assertTrue(
                any(event["payload"]["name"] == "model_roles" for event in ok_events)
            )

    def test_runtime_diagnostics_include_structured_computer_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            observe = ObserveStream(db_path)
            _seed_current_daemon_window(observe, db_path)
            observe.emit(
                "computer_browser_use_timeout",
                payload={"backend": "browser_use", "timeout_seconds": 180},
            )

            report = collect_diagnostics(
                db_path=db_path, port=8765, runner=_healthy_runner, limit=5
            )

            self.assertEqual(report["checks"]["status"], "attention")
            latest = report["database"]["observe"]["latest_errors"]
            self.assertEqual(latest[0]["event_type"], "computer_browser_use_timeout")

    def test_runtime_diagnostics_include_legacy_generic_computer_use_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            observe = ObserveStream(db_path)
            _seed_current_daemon_window(observe, db_path)
            observe.emit(
                "error",
                payload={
                    "source": "computer_use",
                    "error": "browser_use timed out after 180s while executing approved browser automation",
                },
            )

            report = collect_diagnostics(
                db_path=db_path, port=8765, runner=_healthy_runner, limit=5
            )

            self.assertEqual(report["checks"]["status"], "attention")
            latest = report["database"]["observe"]["latest_errors"]
            self.assertEqual(latest[0]["event_type"], "error")
            self.assertEqual(latest[0]["payload"]["source"], "computer_use")


if __name__ == "__main__":
    unittest.main()
