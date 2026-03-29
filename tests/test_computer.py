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


if __name__ == "__main__":
    unittest.main()
