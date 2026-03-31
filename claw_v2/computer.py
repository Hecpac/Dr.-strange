from __future__ import annotations

import base64
import fcntl
import logging
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyautogui

logger = logging.getLogger(__name__)

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.3

LOCK_PATH = Path.home() / ".claw" / "computer_use.lock"
TERMINAL_APPS = {"Terminal", "iTerm2", "Alacritty", "kitty", "Warp", "WezTerm"}


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
    _cancelled: bool = False


@contextmanager
def _computer_use_lock():
    """Exclusive lock file to prevent concurrent Computer Use sessions."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    f = LOCK_PATH.open("w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise RuntimeError("Another Computer Use session is already running")
    try:
        f.write(str(time.time()))
        f.flush()
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
            LOCK_PATH.unlink(missing_ok=True)
        finally:
            f.close()


def _hide_terminal_windows() -> list[str]:
    """Hide terminal windows before screenshot, return names of hidden apps."""
    script = """
    set hiddenApps to {}
    tell application "System Events"
        set allProcs to (name of every process whose visible is true)
    end tell
    set terminalNames to {%s}
    repeat with appName in terminalNames
        if allProcs contains (appName as text) then
            tell application "System Events"
                set visible of process (appName as text) to false
            end tell
            set end of hiddenApps to (appName as text)
        end if
    end repeat
    return hiddenApps
    """ % ", ".join(f'"{a}"' for a in TERMINAL_APPS)
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5,
        )
        return [a.strip() for a in result.stdout.strip().split(",") if a.strip()]
    except Exception:
        logger.warning("Failed to hide terminal windows")
        return []


def _restore_terminal_windows(app_names: list[str]) -> None:
    """Restore previously hidden terminal windows."""
    for name in app_names:
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to set visible of process "{name}" to true'],
                capture_output=True, timeout=5,
            )
        except Exception:
            logger.warning("Failed to restore %s", name)


class _EscListener:
    """Monitors for Escape key press to cancel a Computer Use session."""

    def __init__(self, session: ComputerSession) -> None:
        self._session = session
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        try:
            from pynput import keyboard
        except ImportError:
            logger.warning("pynput not installed — Esc kill switch disabled")
            return

        def on_press(key: Any) -> None:
            if key == keyboard.Key.esc:
                logger.info("Esc pressed — cancelling Computer Use session")
                self._session._cancelled = True
                self._session.status = "cancelled"
                self._stop.set()

        self._listener = keyboard.Listener(on_press=on_press)
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if hasattr(self, "_listener"):
            self._listener.stop()


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

    def capture_screenshot(self, *, exclude_terminals: bool = True) -> dict[str, str]:
        hidden: list[str] = []
        if exclude_terminals:
            hidden = _hide_terminal_windows()
            time.sleep(0.15)  # wait for windows to hide
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
            if hidden:
                _restore_terminal_windows(hidden)

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

    def run_agent_loop(
        self,
        *,
        session: ComputerSession,
        client: Any,
        gate: Any,
        model: str = "claude-opus-4-6",
        system_prompt: str | None = None,
    ) -> str:
        esc_listener = _EscListener(session)
        esc_listener.start()

        try:
            with _computer_use_lock():
                return self._run_loop(
                    session=session, client=client, gate=gate,
                    model=model, system_prompt=system_prompt,
                )
        finally:
            esc_listener.stop()

    def _run_loop(
        self,
        *,
        session: ComputerSession,
        client: Any,
        gate: Any,
        model: str,
        system_prompt: str | None,
    ) -> str:
        if not session.messages:
            screenshot = self.capture_screenshot()
            session.messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": session.task},
                        {"type": "image", "source": {"type": "base64", **screenshot}},
                    ],
                }
            ]
        elif session.pending_action is not None:
            tool_result = self._execute_pending_action(session.pending_action)
            session.messages.append({"role": "user", "content": [tool_result]})
            session.pending_action = None
            session.status = "running"

        while session.iteration < session.max_iterations:
            if session._cancelled:
                session.status = "cancelled"
                return "Session cancelled by Esc key."

            session.iteration += 1
            kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": 4096,
                "tools": [{
                    "type": "computer_20251124",
                    "name": "computer",
                    "display_width_px": self.display_width,
                    "display_height_px": self.display_height,
                }],
                "messages": session.messages,
                "betas": ["computer-use-2025-11-24"],
            }
            if system_prompt:
                kwargs["system"] = system_prompt

            response = client.beta.messages.create(**kwargs)

            response_content = response.content
            session.messages.append({"role": "assistant", "content": response_content})

            tool_uses = [b for b in response_content if b.type == "tool_use"]
            if not tool_uses:
                session.status = "done"
                text_blocks = [b for b in response_content if b.type == "text"]
                return text_blocks[0].text if text_blocks else "(no response)"

            tool_results = []
            for block in tool_uses:
                if session._cancelled:
                    session.status = "cancelled"
                    return "Session cancelled by Esc key."

                action = block.input
                verdict = gate.classify_desktop_action(action, url=session.current_url)

                if verdict.value == "needs_approval":
                    session.status = "awaiting_approval"
                    session.pending_action = {"tool_use_id": block.id, **action}
                    return f"Action needs approval: {action.get('action')} — waiting for /action_approve"

                tool_results.append(self._execute_pending_action({"tool_use_id": block.id, **action}))

            session.messages.append({"role": "user", "content": tool_results})

        session.status = "done"
        return "Computer Use iteration limit reached."

    def _scale_coords(self, coordinate: list[int]) -> tuple[int, int]:
        x = int(coordinate[0] * self.scale_factor)
        y = int(coordinate[1] * self.scale_factor)
        return x, y

    def _execute_pending_action(self, action: dict[str, Any]) -> dict[str, Any]:
        result = self.execute_action(action)
        if result is None:
            result = self.capture_screenshot()
        return {
            "type": "tool_result",
            "tool_use_id": action["tool_use_id"],
            "content": [{"type": "image", "source": {"type": "base64", **result}}],
        }


class BrowserUseService:
    """High-level browser automation via browser-use, complementing ComputerUseService.

    Use this for web tasks (navigate, click by element index, extract data).
    Use ComputerUseService for native desktop apps.
    """

    def __init__(
        self,
        *,
        cdp_url: str | None = "http://localhost:9222",
        headless: bool = True,
    ) -> None:
        self.cdp_url = cdp_url
        self.headless = headless

    async def run_task(
        self,
        task: str,
        *,
        model: str = "gpt-4o",
        max_actions_per_step: int = 5,
        use_vision: bool = True,
        save_conversation: str | None = None,
    ) -> str:
        import os
        from browser_use import Agent, BrowserSession, ChatOpenAI

        browser = BrowserSession(
            cdp_url=self.cdp_url,
            headless=self.headless,
        )
        llm = ChatOpenAI(
            model=model,
            api_key=os.environ.get("OPENAI_API_KEY"),
        )
        agent = Agent(
            task=task,
            llm=llm,
            browser_session=browser,
            max_actions_per_step=max_actions_per_step,
            use_vision=use_vision,
            save_conversation_path=save_conversation,
        )
        try:
            result = await agent.run()
        finally:
            await browser.stop()
        if result.final_result():
            return result.final_result()
        last = result.last_action()
        return str(last) if last else "(no result)"

    async def extract(
        self,
        url: str,
        prompt: str,
        *,
        model: str = "gpt-4o",
    ) -> str:
        """Navigate to a URL and extract information using an LLM."""
        task = f"Go to {url} and extract the following: {prompt}"
        return await self.run_task(task, model=model)

    async def quick_screenshot(self, url: str, output_path: str | None = None) -> str:
        """Take a screenshot of a URL, return base64 or save to path."""
        from browser_use import BrowserSession

        browser = BrowserSession(
            cdp_url=self.cdp_url,
            headless=self.headless,
        )
        async with browser:
            page = await browser.get_current_page()
            await page.goto(url)
            await page.wait_for_load_state("networkidle")
            if output_path:
                await page.screenshot(path=output_path, full_page=True)
                return output_path
            raw = await page.screenshot(full_page=True)
            return base64.b64encode(raw).decode("ascii")


def _resolve_api_key() -> str | None:
    """Resolve Anthropic API key from env or shell profiles."""
    import os, re
    if value := os.getenv("ANTHROPIC_API_KEY"):
        return value.strip() or None
    pattern = re.compile(r"^\s*(?:export\s+)?ANTHROPIC_API_KEY=(?P<value>.+?)\s*$")
    for path in (
        Path.home() / ".zshrc",
        Path.home() / ".zprofile",
        Path.home() / ".zshenv",
        Path.home() / ".profile",
    ):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except FileNotFoundError:
            continue
        for line in reversed(lines):
            match = pattern.match(line)
            if match and (v := match.group("value").strip().strip("\"'")):
                return v
    return None


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
