from __future__ import annotations

import base64
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
            Path(cmd[-1]).write_bytes(fake_png)
            return MagicMock(returncode=0)

        with patch("claw_v2.computer.subprocess.run", side_effect=fake_run) as mock_run:
            with patch("claw_v2.computer._resize_image", return_value=fake_png):
                result = svc.capture_screenshot()

        self.assertTrue(len(result["data"]) > 0)
        self.assertEqual(result["media_type"], "image/png")
        mock_run.assert_called_once()
        self.assertIn("screencapture", mock_run.call_args[0][0])


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


class AgentLoopTests(unittest.TestCase):
    def test_agent_loop_runs_screenshot_then_click_then_completes(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        gate = ActionGate(sensitive_urls=[])
        session = ComputerSession(task="click the button")

        call_count = [0]

        def fake_create(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock(
                    content=[MagicMock(
                        type="tool_use",
                        name="computer",
                        input={"action": "left_click", "coordinate": [500, 300]},
                        id="tool_1",
                    )],
                    stop_reason="tool_use",
                )
            return MagicMock(
                content=[MagicMock(type="text", text="Done! I clicked the button.")],
                stop_reason="end_turn",
            )

        mock_client = MagicMock()
        mock_client.beta.messages.create.side_effect = fake_create

        with patch("claw_v2.computer.pyautogui"):
            with patch.object(svc, "capture_screenshot", return_value={"data": "fake", "media_type": "image/png"}):
                result = svc.run_agent_loop(
                    session=session,
                    client=mock_client,
                    gate=gate,
                    model="claude-opus-4-6",
                )

        self.assertEqual(result, "Done! I clicked the button.")
        self.assertEqual(session.status, "done")
        self.assertEqual(session.iteration, 2)

    def test_agent_loop_stops_at_max_iterations(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        gate = ActionGate(sensitive_urls=[])
        session = ComputerSession(task="infinite task", max_iterations=2)

        def always_tool_use(**kwargs):
            return MagicMock(
                content=[MagicMock(
                    type="tool_use",
                    name="computer",
                    input={"action": "screenshot"},
                    id="tool_x",
                )],
                stop_reason="tool_use",
            )

        mock_client = MagicMock()
        mock_client.beta.messages.create.side_effect = always_tool_use

        with patch.object(svc, "capture_screenshot", return_value={"data": "fake", "media_type": "image/png"}):
            result = svc.run_agent_loop(
                session=session,
                client=mock_client,
                gate=gate,
                model="claude-opus-4-6",
            )

        self.assertIn("limit", result.lower())
        self.assertEqual(session.iteration, 2)

    def test_agent_loop_pauses_when_gate_needs_approval(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        gate = ActionGate(sensitive_urls=["ads.google.com"])
        session = ComputerSession(task="click buy", current_url="https://ads.google.com")

        def fake_create(**kwargs):
            return MagicMock(
                content=[MagicMock(
                    type="tool_use",
                    name="computer",
                    input={"action": "left_click", "coordinate": [500, 300]},
                    id="tool_1",
                )],
                stop_reason="tool_use",
            )

        mock_client = MagicMock()
        mock_client.beta.messages.create.side_effect = fake_create

        with patch.object(svc, "capture_screenshot", return_value={"data": "fake", "media_type": "image/png"}):
            result = svc.run_agent_loop(
                session=session,
                client=mock_client,
                gate=gate,
                model="claude-opus-4-6",
            )

        self.assertEqual(session.status, "awaiting_approval")
        self.assertIsNotNone(session.pending_action)
        self.assertIn("approval", result.lower())


if __name__ == "__main__":
    unittest.main()
