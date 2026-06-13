from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

from claw_v2.computer import ComputerUseService, ComputerSession


class ScreenshotTests(unittest.TestCase):
    def test_capture_calls_screencapture_and_returns_base64(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        def fake_run(cmd, **kwargs):
            if cmd[0] == "screencapture":
                Path(cmd[-1]).write_bytes(fake_png)
            return MagicMock(returncode=0)

        with patch("claw_v2.computer.subprocess.run", side_effect=fake_run) as mock_run:
            with patch("claw_v2.computer._resize_image", return_value=fake_png):
                result = svc.capture_screenshot()

        self.assertTrue(len(result["data"]) > 0)
        self.assertEqual(result["media_type"], "image/png")
        screencapture_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "screencapture"]
        self.assertEqual(len(screencapture_calls), 1)


class ActionExecutorTests(unittest.TestCase):
    def test_click_scales_coordinates(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800, scale_factor=2.0)
        with patch("claw_v2.computer.pyautogui") as mock_pag:
            svc.execute_action({"action": "left_click", "coordinate": [640, 400]})
        mock_pag.click.assert_called_once_with(1280, 800)

    def test_type_action(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        with patch("claw_v2.computer.pyautogui") as mock_pag:
            svc.execute_action({"action": "type", "text": "hello world"})
        mock_pag.typewrite.assert_called_once_with("hello world", interval=0.02)

    def test_key_action_single(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        with patch("claw_v2.computer.pyautogui") as mock_pag:
            svc.execute_action({"action": "key", "text": "Escape"})
        mock_pag.press.assert_called_once_with("Escape")

    def test_key_action_hotkey(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        with patch("claw_v2.computer.pyautogui") as mock_pag:
            svc.execute_action({"action": "key", "text": "cmd+t"})
        mock_pag.hotkey.assert_called_once_with("cmd", "t")

    def test_scroll_action(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        with patch("claw_v2.computer.pyautogui") as mock_pag:
            svc.execute_action({"action": "scroll", "coordinate": [500, 400], "scroll_direction": "down", "scroll_amount": 3})
        mock_pag.moveTo.assert_called_once()
        mock_pag.scroll.assert_called_once_with(-3)

    def test_mouse_move_scales(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800, scale_factor=2.0)
        with patch("claw_v2.computer.pyautogui") as mock_pag:
            svc.execute_action({"action": "mouse_move", "coordinate": [100, 200]})
        mock_pag.moveTo.assert_called_once_with(200, 400)

    def test_screenshot_action_calls_capture(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        with patch.object(svc, "capture_screenshot", return_value={"data": "abc", "media_type": "image/png"}) as mock_cap:
            result = svc.execute_action({"action": "screenshot"})
        mock_cap.assert_called_once()
        self.assertEqual(result["data"], "abc")

    def test_right_click(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        with patch("claw_v2.computer.pyautogui") as mock_pag:
            svc.execute_action({"action": "right_click", "coordinate": [100, 200]})
        mock_pag.rightClick.assert_called_once_with(100, 200)

    def test_double_click(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        with patch("claw_v2.computer.pyautogui") as mock_pag:
            svc.execute_action({"action": "double_click", "coordinate": [100, 200]})
        mock_pag.doubleClick.assert_called_once_with(100, 200)


class ComputerSessionTests(unittest.TestCase):
    def test_session_defaults(self) -> None:
        session = ComputerSession(task="test task")
        self.assertEqual(session.status, "running")
        self.assertEqual(session.iteration, 0)
        self.assertEqual(session.max_iterations, 30)
        self.assertIsNone(session.pending_action)
        self.assertIsNone(session.current_url)


from claw_v2.computer_gate import ActionGate


def _mock_openai_response(*, computer_calls=None, text=None, response_id="resp_1"):
    """Build a mock OpenAI Responses API response."""
    output = []
    if computer_calls:
        for call in computer_calls:
            action_mock = MagicMock()
            action_mock.model_dump.return_value = call["action"]
            output.append(MagicMock(type="computer_call", call_id=call["call_id"], action=action_mock))
    if text:
        output.append(MagicMock(type="text", text=text))
    return MagicMock(id=response_id, output=output)


class AgentLoopTests(unittest.TestCase):
    def test_agent_loop_runs_screenshot_then_click_then_completes(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        gate = ActionGate(sensitive_urls=[])
        session = ComputerSession(task="click the button", current_url="https://docs.google.com")

        call_count = [0]

        def fake_create(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _mock_openai_response(
                    computer_calls=[{"call_id": "call_1", "action": {"type": "click", "x": 500, "y": 300, "button": "left"}}],
                    response_id="resp_1",
                )
            return _mock_openai_response(text="Done! I clicked the button.", response_id="resp_2")

        mock_client = MagicMock()
        mock_client.responses.create.side_effect = fake_create

        with patch("claw_v2.computer.pyautogui"):
            with patch.object(svc, "capture_screenshot", return_value={"data": "fake", "media_type": "image/png"}):
                result = svc.run_agent_loop(
                    session=session,
                    client=mock_client,
                    gate=gate,
                    model="computer-use-preview",
                )

        self.assertEqual(result, "Done! I clicked the button.")
        self.assertEqual(session.status, "done")
        self.assertEqual(session.iteration, 2)
        self.assertEqual(session.visual_checks, 1)
        self.assertFalse(session.last_visual_changed)

    def test_agent_loop_stops_at_max_iterations(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        gate = ActionGate(sensitive_urls=[])
        session = ComputerSession(task="infinite task", max_iterations=2, current_url="https://example.com")

        def always_computer_call(**kwargs):
            return _mock_openai_response(
                computer_calls=[{"call_id": "call_x", "action": {"type": "screenshot"}}],
            )

        mock_client = MagicMock()
        mock_client.responses.create.side_effect = always_computer_call

        with patch.object(svc, "capture_screenshot", return_value={"data": "fake", "media_type": "image/png"}):
            result = svc.run_agent_loop(
                session=session,
                client=mock_client,
                gate=gate,
                model="computer-use-preview",
            )

        self.assertIn("limit", result.lower())
        self.assertEqual(session.iteration, 2)

    def test_agent_loop_pauses_when_gate_needs_approval(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        gate = ActionGate(sensitive_urls=["ads.google.com"])
        session = ComputerSession(task="click buy", current_url="https://ads.google.com")

        def fake_create(**kwargs):
            return _mock_openai_response(
                computer_calls=[{"call_id": "call_1", "action": {"type": "click", "x": 500, "y": 300, "button": "left"}}],
            )

        mock_client = MagicMock()
        mock_client.responses.create.side_effect = fake_create

        with patch.object(svc, "capture_screenshot", return_value={"data": "fake", "media_type": "image/png"}):
            result = svc.run_agent_loop(
                session=session,
                client=mock_client,
                gate=gate,
                model="computer-use-preview",
            )

        self.assertEqual(session.status, "awaiting_approval")
        self.assertIsNotNone(session.pending_action)
        self.assertIn("approval", result.lower())

    def test_agent_loop_resumes_pending_action_before_next_model_turn(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        gate = ActionGate(sensitive_urls=[])
        session = ComputerSession(
            task="click continue",
            messages=[{"role": "user", "content": [{"type": "input_text", "text": "click continue"}]}],
            pending_action={"call_id": "call_1", "type": "click", "x": 500, "y": 300, "button": "left"},
            status="running",
        )

        mock_client = MagicMock()
        mock_client.responses.create.return_value = _mock_openai_response(
            text="Done after approval.", response_id="resp_2",
        )

        with patch.object(svc, "execute_action", return_value={"data": "fake", "media_type": "image/png"}) as mock_exec:
            with patch.object(svc, "capture_screenshot", return_value={"data": "fake", "media_type": "image/png"}):
                result = svc.run_agent_loop(
                    session=session,
                    client=mock_client,
                    gate=gate,
                    model="computer-use-preview",
                )

        self.assertEqual(result, "Done after approval.")
        self.assertIsNone(session.pending_action)
        self.assertEqual(session.status, "done")
        mock_exec.assert_called_once()
        # Check the call_output was appended
        call_output = session.messages[-1]
        self.assertEqual(call_output["type"], "computer_call_output")
        self.assertEqual(call_output["call_id"], "call_1")

    def test_agent_loop_tracks_visual_change_after_action(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        gate = ActionGate(sensitive_urls=[])
        session = ComputerSession(task="click the button", current_url="https://example.com")

        call_count = [0]

        def fake_create(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _mock_openai_response(
                    computer_calls=[{"call_id": "call_1", "action": {"type": "click", "x": 500, "y": 300, "button": "left"}}],
                    response_id="resp_1",
                )
            return _mock_openai_response(text="Done.", response_id="resp_2")

        screenshots = [
            {"data": base64.b64encode(b"before").decode("ascii"), "media_type": "image/png"},
            {"data": base64.b64encode(b"after").decode("ascii"), "media_type": "image/png"},
        ]
        mock_client = MagicMock()
        mock_client.responses.create.side_effect = fake_create

        with patch("claw_v2.computer.pyautogui"):
            with patch.object(svc, "capture_screenshot", side_effect=screenshots):
                result = svc.run_agent_loop(
                    session=session,
                    client=mock_client,
                    gate=gate,
                    model="computer-use-preview",
                )

        self.assertEqual(result, "Done.")
        self.assertEqual(session.visual_checks, 1)
        self.assertTrue(session.last_visual_changed)


class BrowserUseServiceTests(unittest.TestCase):
    def test_init_defaults(self) -> None:
        from claw_v2.computer import BrowserUseService
        svc = BrowserUseService()
        self.assertEqual(svc.cdp_url, "http://localhost:9250")
        self.assertTrue(svc.headless)

    def test_init_custom(self) -> None:
        from claw_v2.computer import BrowserUseService
        svc = BrowserUseService(cdp_url="http://localhost:9333", headless=False)
        self.assertEqual(svc.cdp_url, "http://localhost:9333")
        self.assertFalse(svc.headless)


class CodexComputerBackendTests(unittest.TestCase):
    def test_transport_override_returns_fixed_result(self) -> None:
        from claw_v2.computer import CodexComputerBackend
        backend = CodexComputerBackend(transport=lambda task: "done: opened Chrome")
        result = backend.run("open Chrome")
        self.assertEqual(result, "done: opened Chrome")

    def test_raises_runtime_error_when_cli_not_found(self) -> None:
        from claw_v2.computer import CodexComputerBackend
        backend = CodexComputerBackend(cli_path="nonexistent-codex-xyz")
        with self.assertRaises(RuntimeError):
            backend.run("open Chrome")

    def test_raises_runtime_error_on_nonzero_exit(self) -> None:
        from claw_v2.computer import CodexComputerBackend
        from unittest.mock import patch, MagicMock
        backend = CodexComputerBackend(cli_path="codex")
        fake_result = MagicMock(returncode=1, stdout="", stderr="failed")
        with patch("claw_v2.computer.subprocess.run", return_value=fake_result):
            with patch("claw_v2.computer.shutil.which", return_value="/usr/local/bin/codex"):
                with self.assertRaises(RuntimeError):
                    backend.run("open Chrome")

    def test_successful_run_returns_stdout(self) -> None:
        from claw_v2.computer import CodexComputerBackend
        from unittest.mock import patch, MagicMock
        backend = CodexComputerBackend(cli_path="codex", model="codex-mini-latest")
        fake_result = MagicMock(returncode=0, stdout="Opened Chrome successfully.\n", stderr="")
        with patch("claw_v2.computer.subprocess.run", return_value=fake_result):
            with patch("claw_v2.computer.shutil.which", return_value="/usr/local/bin/codex"):
                result = backend.run("open Chrome")
        self.assertEqual(result, "Opened Chrome successfully.")

    def test_computer_use_service_pauses_codex_backend_for_approval(self) -> None:
        from claw_v2.computer import CodexComputerBackend, ComputerUseService, ComputerSession
        calls: list[str] = []
        backend = CodexComputerBackend(transport=lambda task: calls.append(task) or f"completed: {task}")
        svc = ComputerUseService(display_width=1280, display_height=800, codex_backend=backend)
        session = ComputerSession(task="open Safari")
        result = svc.run_agent_loop(session=session, client=None, gate=None, model="codex-mini-latest")
        self.assertIn("needs approval", result)
        self.assertEqual(session.status, "awaiting_approval")
        self.assertEqual(session.pending_action["action"], "codex_computer_task")
        self.assertEqual(calls, [])

    def test_computer_use_service_runs_codex_backend_after_approval(self) -> None:
        from claw_v2.computer import CodexComputerBackend, ComputerUseService, ComputerSession
        calls: list[str] = []
        backend = CodexComputerBackend(transport=lambda task: calls.append(task) or f"completed: {task}")
        svc = ComputerUseService(display_width=1280, display_height=800, codex_backend=backend)
        session = ComputerSession(
            task="open Safari",
            pending_action={
                "action": "codex_computer_task",
                "approved": True,
                "approval_id": "approval-1",
            },
        )
        result = svc.run_agent_loop(session=session, client=None, gate=None, model="codex-mini-latest")
        self.assertEqual(result, "completed: open Safari")
        self.assertEqual(session.status, "done")
        self.assertIsNone(session.pending_action)
        self.assertEqual(calls, ["open Safari"])


class BrowserUseModelTests(unittest.TestCase):
    def _capture_model(self, explicit: str | None = None) -> str:
        import asyncio
        import sys
        import types

        from claw_v2.computer import BrowserUseService

        captured: dict = {}

        class FakeChatOpenAI:
            def __init__(self, **kwargs):
                captured["model"] = kwargs.get("model")

        class FakeBrowserSession:
            def __init__(self, **kwargs):
                pass

            async def stop(self):
                pass

        class FakeResult:
            def final_result(self):
                return "ok"

            def last_action(self):
                return None

        class FakeAgent:
            def __init__(self, **kwargs):
                pass

            async def run(self):
                return FakeResult()

        module = types.SimpleNamespace(
            Agent=FakeAgent, BrowserSession=FakeBrowserSession, ChatOpenAI=FakeChatOpenAI
        )
        with patch.dict(sys.modules, {"browser_use": module}):
            svc = BrowserUseService()
            if explicit is None:
                asyncio.run(svc.run_task("t"))
            else:
                asyncio.run(svc.run_task("t", model=explicit))
        return captured["model"]

    def test_default_model_updated_from_gpt4o(self) -> None:
        from claw_v2.computer import DEFAULT_BROWSER_USE_MODEL

        self.assertEqual(DEFAULT_BROWSER_USE_MODEL, "gpt-5.4")
        self.assertEqual(self._capture_model(), "gpt-5.4")

    def test_explicit_model_passed_through(self) -> None:
        self.assertEqual(self._capture_model(explicit="gpt-5.5"), "gpt-5.5")


class _ComputerHandlerConfigTest(unittest.TestCase):
    def _handler(self, config):
        from claw_v2.computer_handler import ComputerHandler

        return ComputerHandler(config=config)


class ComputerHandlerModelTests(_ComputerHandlerConfigTest):

    def test_reads_config_model(self) -> None:
        import types

        cfg = types.SimpleNamespace(computer_browser_use_model="gpt-5.5")
        self.assertEqual(self._handler(cfg)._browser_use_model(), "gpt-5.5")

    def test_falls_back_to_default_when_missing_or_blank(self) -> None:
        import types

        from claw_v2.computer import DEFAULT_BROWSER_USE_MODEL

        self.assertEqual(self._handler(None)._browser_use_model(), DEFAULT_BROWSER_USE_MODEL)
        self.assertEqual(
            self._handler(types.SimpleNamespace())._browser_use_model(), DEFAULT_BROWSER_USE_MODEL
        )
        self.assertEqual(
            self._handler(types.SimpleNamespace(computer_browser_use_model="  "))._browser_use_model(),
            DEFAULT_BROWSER_USE_MODEL,
        )


class BrowserUseArtifactTests(unittest.TestCase):
    def _fakes(self, *, screenshot_raises: bool = False, final: str = "imagen creada"):
        import types

        events: dict = {}

        class FakeBrowserSession:
            def __init__(self, **kwargs):
                pass

            async def take_screenshot(self, path=None, full_page=False):
                if screenshot_raises:
                    raise RuntimeError("cdp screenshot boom")
                Path(path).write_bytes(b"PNGDATA")
                events["screenshot_path"] = path
                return b"PNGDATA"

            async def stop(self):
                events["stopped"] = True

        class FakeResult:
            def final_result(self):
                return final

            def last_action(self):
                return None

        class FakeAgent:
            def __init__(self, **kwargs):
                pass

            async def run(self):
                return FakeResult()

        class FakeChatOpenAI:
            def __init__(self, **kwargs):
                pass

        module = types.SimpleNamespace(
            Agent=FakeAgent, BrowserSession=FakeBrowserSession, ChatOpenAI=FakeChatOpenAI
        )
        return module, events

    def test_run_task_saves_screenshot_and_returns_path(self) -> None:
        import asyncio
        import sys
        from claw_v2.computer import BrowserUseService

        module, events = self._fakes()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(sys.modules, {"browser_use": module}):
                svc = BrowserUseService()
                result = asyncio.run(svc.run_task("crea imagen", artifact_dir=tmp))
            self.assertIn("imagen creada", result)
            self.assertTrue(events.get("stopped"))
            self.assertIsNotNone(svc.last_artifact_path)
            saved = Path(svc.last_artifact_path)
            self.assertTrue(saved.exists())
            self.assertEqual(saved.parent, Path(tmp))
            self.assertIn(str(saved), result)

    def test_screenshot_failure_still_returns_text(self) -> None:
        import asyncio
        import sys
        from claw_v2.computer import BrowserUseService

        module, _ = self._fakes(screenshot_raises=True, final="texto resultado")
        with patch.dict(sys.modules, {"browser_use": module}):
            svc = BrowserUseService()
            result = asyncio.run(svc.run_task("hola"))
        self.assertEqual(result, "texto resultado")
        self.assertIsNone(svc.last_artifact_path)

    def test_agent_timeout_raises(self) -> None:
        import asyncio
        import sys
        import types

        from claw_v2.computer import BrowserUseService

        class FakeBrowserSession:
            def __init__(self, **kwargs):
                pass

            async def stop(self):
                pass

        class FakeAgent:
            def __init__(self, **kwargs):
                pass

            async def run(self):
                await asyncio.sleep(1)
                return None

        class FakeChatOpenAI:
            def __init__(self, **kwargs):
                pass

        module = types.SimpleNamespace(
            Agent=FakeAgent, BrowserSession=FakeBrowserSession, ChatOpenAI=FakeChatOpenAI
        )
        with patch.dict(sys.modules, {"browser_use": module}):
            svc = BrowserUseService()
            with self.assertRaises(asyncio.TimeoutError):
                asyncio.run(svc.run_task("t", timeout=0.05))

    def test_slow_capture_does_not_fail_completed_task(self) -> None:
        # A completed agent task near the timeout must not be turned into a
        # failure by a slow/hung screenshot capture (Codex review).
        import asyncio
        import sys
        import types

        import claw_v2.computer as computer_mod
        from claw_v2.computer import BrowserUseService

        class FakeBrowserSession:
            def __init__(self, **kwargs):
                pass

            async def take_screenshot(self, path=None, full_page=False):
                await asyncio.sleep(1)  # exceeds the patched capture budget

            async def stop(self):
                pass

        class FakeResult:
            def final_result(self):
                return "completado"

            def last_action(self):
                return None

        class FakeAgent:
            def __init__(self, **kwargs):
                pass

            async def run(self):
                return FakeResult()

        class FakeChatOpenAI:
            def __init__(self, **kwargs):
                pass

        module = types.SimpleNamespace(
            Agent=FakeAgent, BrowserSession=FakeBrowserSession, ChatOpenAI=FakeChatOpenAI
        )
        with patch.object(computer_mod, "_BROWSER_USE_CAPTURE_TIMEOUT_SECONDS", 0.05):
            with patch.dict(sys.modules, {"browser_use": module}):
                svc = BrowserUseService()
                result = asyncio.run(svc.run_task("t", timeout=5))
        self.assertEqual(result, "completado")
        self.assertIsNone(svc.last_artifact_path)

    def test_run_task_passes_domain_policy_to_browser_session(self) -> None:
        import asyncio
        import sys
        import types
        from claw_v2.computer import BrowserUseService

        created: dict = {}

        class FakeBrowserSession:
            def __init__(self, **kwargs):
                created.update(kwargs)

            async def stop(self):
                pass

        class FakeResult:
            def final_result(self):
                return "ok"

            def last_action(self):
                return None

        class FakeAgent:
            def __init__(self, **kwargs):
                pass

            async def run(self):
                return FakeResult()

        class FakeChatOpenAI:
            def __init__(self, **kwargs):
                pass

        module = types.SimpleNamespace(
            Agent=FakeAgent, BrowserSession=FakeBrowserSession, ChatOpenAI=FakeChatOpenAI
        )
        with patch.dict(sys.modules, {"browser_use": module}):
            svc = BrowserUseService()
            result = asyncio.run(
                svc.run_task(
                    "t",
                    allowed_domains=["https://chatgpt.com/"],
                    prohibited_domains=["https://stripe.com/dashboard"],
                )
            )
        self.assertEqual(result, "ok")
        self.assertEqual(created["allowed_domains"], ["chatgpt.com"])
        self.assertEqual(created["prohibited_domains"], ["stripe.com"])


class BrowserUseGuardTests(unittest.TestCase):
    def test_guarded_tools_import_does_not_mutate_claw_model_env(self) -> None:
        import os

        from claw_v2.computer import BrowserUseService

        keys = (
            "BRAIN_MODEL",
            "WORKER_MODEL",
            "JUDGE_MODEL",
            "CLAW_BUDGET_CAP_DAILY",
            "TELEGRAM_BOT_TOKEN",
            "OPENAI_API_KEY",
            "GOOGLE_API_KEY",
        )
        original = {key: os.environ.get(key) for key in keys}
        for key in keys:
            os.environ.pop(key, None)
        try:
            BrowserUseService()._guarded_browser_tools(
                action_gate=ActionGate(sensitive_urls=[]),
                approved_domains=[],
                allow_high_risk_actions=False,
            )
            self.assertEqual({key: os.environ.get(key) for key in keys}, {key: None for key in keys})
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_guarded_tools_blocks_high_risk_sensitive_navigation_before_execution(self) -> None:
        import asyncio

        from claw_v2.computer import BrowserUsePolicyInterrupt, BrowserUseService

        class FakeAction:
            def model_dump(self, **kwargs):
                return {"navigate": {"url": "https://robinhood.com/account"}}

        class FakeBrowserSession:
            async def get_current_page_url(self):
                return "https://example.com"

        svc = BrowserUseService()
        gate = ActionGate(sensitive_urls=["robinhood.com"], auto_approve=True)
        tools, state = svc._guarded_browser_tools(
            action_gate=gate,
            approved_domains=[],
            allow_high_risk_actions=False,
        )

        result = asyncio.run(tools.act(action=FakeAction(), browser_session=FakeBrowserSession()))

        self.assertIn("requires approval", result.error)
        self.assertTrue(state["should_stop"])
        self.assertIsInstance(state["interrupt"], BrowserUsePolicyInterrupt)
        self.assertEqual(state["interrupt"].action_name, "navigate")


class ComputerHandlerSessionArtifactTests(unittest.TestCase):
    def test_run_browser_use_task_binds_artifact_to_session(self) -> None:
        # The artifact is read from the session (set inside the worker thread),
        # not from the shared service attribute — avoids the concurrent-session
        # race (Gemini review).
        import types

        from claw_v2.computer_handler import ComputerHandler

        class FakeBrowserUse:
            def __init__(self):
                self.last_artifact_path = None

            async def run_task(self, task, **kwargs):
                self.last_artifact_path = "/tmp/img-A.png"
                return "ok"

        handler = ComputerHandler(browser_use=FakeBrowserUse(), config=None)
        session = types.SimpleNamespace(task="hola", screenshot_path=None)
        result = handler._run_browser_use_task(session)
        self.assertEqual(result, "ok")
        self.assertEqual(session.screenshot_path, "/tmp/img-A.png")

    def test_browser_use_policy_interrupt_becomes_pending_approval(self) -> None:
        import types

        from claw_v2.computer import BrowserUsePolicyInterrupt
        from claw_v2.computer_handler import ComputerHandler

        class FakeBrowserUse:
            last_artifact_path = None

            async def run_task(self, task, **kwargs):
                raise BrowserUsePolicyInterrupt(
                    action_name="navigate",
                    params={"url": "https://robinhood.com/account"},
                    url="https://example.com",
                    risk="high",
                    approved_domains=["robinhood.com"],
                )

        config = types.SimpleNamespace(computer_auto_approve=True, sensitive_urls=["robinhood.com"])
        handler = ComputerHandler(browser_use=FakeBrowserUse(), config=config)
        session = types.SimpleNamespace(
            task="open a normal website then continue",
            current_url="https://example.com",
            status="running",
            pending_action={"action": "browser_use_task", "backend": "browser_use", "task": "open"},
        )

        result = handler._run_browser_use_session(session)

        self.assertIn("needs approval", result)
        self.assertEqual(session.status, "awaiting_approval")
        self.assertEqual(session.pending_action["interrupted_action"]["action"], "navigate")
        self.assertEqual(session.pending_action["approved_domains"], ["robinhood.com"])

    def test_resume_blocks_when_approval_screenshot_hash_changed(self) -> None:
        import types

        from claw_v2.approval import ApprovalManager
        from claw_v2.computer_handler import ComputerHandler

        class FakeComputer:
            def capture_screenshot(self):
                return {
                    "data": base64.b64encode(b"after").decode("ascii"),
                    "media_type": "image/png",
                }

            def run_agent_loop(self, **kwargs):
                raise AssertionError("must not execute after changed screenshot")

        with tempfile.TemporaryDirectory() as tmpdir:
            approvals = ApprovalManager(Path(tmpdir), "secret")
            session = types.SimpleNamespace(
                task="click",
                current_url="https://example.com",
                status="awaiting_approval",
                pending_action={"action": "click", "x": 1, "y": 2},
            )
            scope = {
                "backend": "openai",
                "action_hash": hashlib.sha256(
                    json.dumps(session.pending_action, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "current_url": "https://example.com",
                "url_origin": "https://example.com",
                "screenshot_hash": hashlib.sha256(b"before").hexdigest(),
                "approved_domains": [],
            }
            pending = approvals.create(
                "click",
                "click",
                metadata={"kind": "computer_use", "session_id": "s1", "approval_scope": scope},
            )
            session.pending_action["approval_id"] = pending.approval_id
            handler = ComputerHandler(computer=FakeComputer(), approvals=approvals, config=None)
            handler._sessions["s1"] = session
            approvals.approve(pending.approval_id, pending.token)

            result = handler._resume_approved_computer_action(pending.approval_id)

        self.assertIn("contexto de computer cambió", result)

    def test_approval_blocked_when_screenshot_capture_fails(self) -> None:
        # Fail-closed: if the approval screenshot can't be captured, the action
        # has no anti-TOCTOU visual binding, so no approval is created.
        import types

        from claw_v2.computer_handler import ComputerHandler

        class FakeComputer:
            codex_backend = object()  # non-None -> handler skips _get_client()

            def capture_screenshot(self):
                raise RuntimeError("cdp screenshot boom")

            def run_agent_loop(self, *, session, **kwargs):
                session.status = "awaiting_approval"
                session.pending_action = {"action": "click", "x": 1, "y": 2}
                return "paused"

        events: list[tuple[str, dict]] = []

        class FakeObserve:
            def emit(self, event_type, payload=None):
                events.append((event_type, payload or {}))

        approvals = MagicMock()
        config = types.SimpleNamespace(computer_auto_approve=False, sensitive_urls=[])
        handler = ComputerHandler(
            computer=FakeComputer(),
            approvals=approvals,
            config=config,
            observe=FakeObserve(),
            computer_gate=object(),
        )
        session = types.SimpleNamespace(
            task="click something",
            current_url="https://example.com",
            status="running",
            pending_action={"action": "click", "x": 1, "y": 2},
            screenshot_path=None,
        )
        handler._sessions["s1"] = session

        result = handler._run_session("s1")

        approvals.create.assert_not_called()
        self.assertEqual(session.status, "aborted")
        self.assertNotIn("s1", handler._sessions)
        self.assertTrue(
            any(e[0] == "computer_approval_blocked_no_screenshot" for e in events),
            f"events={[e[0] for e in events]}",
        )
        self.assertIn("aprobación segura", result)

    def test_browser_use_approval_not_blocked_without_screenshot_backend(self) -> None:
        # Regression: browser_use-only deploys (computer=None) have no desktop
        # screenshot backend, so the fail-closed check must NOT block their
        # approvals (they never had screenshot binding to begin with).
        import types

        from claw_v2.computer_handler import ComputerHandler

        events: list[tuple[str, dict]] = []

        class FakeObserve:
            def emit(self, event_type, payload=None):
                events.append((event_type, payload or {}))

        approvals = MagicMock()
        approvals.create.return_value = types.SimpleNamespace(approval_id="a1", token="t1")
        config = types.SimpleNamespace(computer_auto_approve=False, sensitive_urls=[])
        handler = ComputerHandler(
            browser_use=object(),
            computer=None,
            approvals=approvals,
            config=config,
            observe=FakeObserve(),
        )
        session = types.SimpleNamespace(
            task="navega y continúa",
            current_url="https://example.com",
            status="running",
            pending_action={"action": "browser_use_task", "backend": "browser_use"},
            screenshot_path=None,
        )
        handler._sessions["s1"] = session

        def fake_browser_use(sess):
            sess.status = "awaiting_approval"
            sess.pending_action = {
                "action": "browser_use_task",
                "backend": "browser_use",
                "interrupted_action": {"action": "navigate"},
            }
            return "needs approval"

        with patch.object(handler, "_run_browser_use_session", side_effect=fake_browser_use):
            result = handler._run_session("s1")

        approvals.create.assert_called_once()
        self.assertNotIn("aprobación segura", result)
        self.assertFalse(
            any(e[0] == "computer_approval_blocked_no_screenshot" for e in events)
        )


class DelegatedBrowserTaskTests(unittest.TestCase):
    """Option (b), 2026-06-13: ComputerHandler.run_delegated_browser_task is the
    in-process executor TaskHandler routes CDP/browse jobs to (BrowserUseService
    in the daemon venv, not the network-denied Codex coordinator)."""

    def test_runs_browser_use_with_long_timeout(self) -> None:
        import types

        from claw_v2.computer_handler import ComputerHandler

        class FakeBrowserUse:
            last_artifact_path = None

            def __init__(self) -> None:
                self.calls: list[tuple[str, object]] = []

            async def run_task(self, task, **kwargs):
                self.calls.append((task, kwargs.get("timeout")))
                return "feed capturado: 30 posts"

        fake = FakeBrowserUse()
        config = types.SimpleNamespace(
            computer_auto_approve=True,
            sensitive_urls=[],
            computer_browser_use_timeout_seconds=0,
        )
        handler = ComputerHandler(browser_use=fake, config=config)
        out = handler.run_delegated_browser_task("repaso por X", task_id="t-1", mode="browse")
        self.assertEqual(out, "feed capturado: 30 posts")
        self.assertEqual(fake.calls[0][0], "repaso por X")
        # Long browser/CDP budget (1200s), NOT the 180s interactive default.
        self.assertEqual(fake.calls[0][1], 1200)

    def test_unavailable_browser_use_returns_clear_message(self) -> None:
        from claw_v2.computer_handler import ComputerHandler

        handler = ComputerHandler(browser_use=None, config=None)
        out = handler.run_delegated_browser_task("abre la web", task_id="t-2", mode="browse")
        self.assertIn("no está disponible", out)


class ComputerHandlerTimeoutTests(_ComputerHandlerConfigTest):

    def test_timeout_defaults_to_constant_without_config(self) -> None:
        from claw_v2.computer_handler import BROWSER_USE_TIMEOUT_SECONDS

        self.assertEqual(self._handler(None)._browser_use_timeout(), BROWSER_USE_TIMEOUT_SECONDS)

    def test_timeout_reads_config_value(self) -> None:
        import types

        cfg = types.SimpleNamespace(computer_browser_use_timeout_seconds=600)
        self.assertEqual(self._handler(cfg)._browser_use_timeout(), 600)

    def test_timeout_falls_back_when_missing_or_nonpositive(self) -> None:
        import types

        from claw_v2.computer_handler import BROWSER_USE_TIMEOUT_SECONDS

        self.assertEqual(
            self._handler(types.SimpleNamespace())._browser_use_timeout(),
            BROWSER_USE_TIMEOUT_SECONDS,
        )
        cfg0 = types.SimpleNamespace(computer_browser_use_timeout_seconds=0)
        self.assertEqual(self._handler(cfg0)._browser_use_timeout(), BROWSER_USE_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
