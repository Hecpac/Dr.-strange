from __future__ import annotations

import base64
import logging
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyautogui

logger = logging.getLogger(__name__)

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.3


@dataclass
class ComputerSession:
    task: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    status: str = "running"
    pending_action: dict[str, Any] | None = None
    screenshot_path: str | None = None
    max_iterations: int = 30
    iteration: int = 0
    current_url: str | None = None


class ComputerUseService:
    def __init__(
        self,
        *,
        display_width: int = 1280,
        display_height: int = 800,
        scale_factor: float = 1.0,
        action_delay: float = 0.3,
    ) -> None:
        self.display_width = display_width
        self.display_height = display_height
        self.scale_factor = scale_factor
        self.action_delay = action_delay

    def capture_screenshot(self) -> dict[str, str]:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            subprocess.run(
                ["screencapture", "-x", tmp_path],
                check=True,
                capture_output=True,
                timeout=10,
            )
            raw = Path(tmp_path).read_bytes()
            resized = _resize_image(raw, self.display_width, self.display_height)
            encoded = base64.b64encode(resized).decode("ascii")
            return {"data": encoded, "media_type": "image/png"}
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def execute_action(self, action: dict[str, Any]) -> dict[str, str] | None:
        action_type = action.get("action", "")
        if action_type == "screenshot":
            return self.capture_screenshot()
        if action_type == "left_click":
            x, y = self._scale_coords(action["coordinate"])
            pyautogui.click(x, y)
        elif action_type == "right_click":
            x, y = self._scale_coords(action["coordinate"])
            pyautogui.rightClick(x, y)
        elif action_type == "double_click":
            x, y = self._scale_coords(action["coordinate"])
            pyautogui.doubleClick(x, y)
        elif action_type == "middle_click":
            x, y = self._scale_coords(action["coordinate"])
            pyautogui.middleClick(x, y)
        elif action_type == "type":
            pyautogui.typewrite(action["text"], interval=0.02)
        elif action_type == "key":
            keys = action["text"].split("+")
            if len(keys) > 1:
                pyautogui.hotkey(*keys)
            else:
                pyautogui.press(keys[0])
        elif action_type == "mouse_move":
            x, y = self._scale_coords(action["coordinate"])
            pyautogui.moveTo(x, y)
        elif action_type == "scroll":
            x, y = self._scale_coords(action.get("coordinate", [0, 0]))
            pyautogui.moveTo(x, y)
            direction = action.get("scroll_direction", "down")
            amount = action.get("scroll_amount", 3)
            scroll_val = -amount if direction == "down" else amount
            pyautogui.scroll(scroll_val)
        elif action_type == "left_click_drag":
            start = self._scale_coords(action["start_coordinate"])
            end = self._scale_coords(action["coordinate"])
            pyautogui.moveTo(start[0], start[1])
            pyautogui.drag(end[0] - start[0], end[1] - start[1])
        else:
            logger.warning("Unknown action type: %s", action_type)
        time.sleep(self.action_delay)
        return None

    def _scale_coords(self, coordinate: list[int]) -> tuple[int, int]:
        x = int(coordinate[0] * self.scale_factor)
        y = int(coordinate[1] * self.scale_factor)
        return x, y


def _resize_image(raw: bytes, width: int, height: int) -> bytes:
    try:
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(raw))
        img = img.resize((width, height), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        return raw
