from __future__ import annotations

import base64
import fcntl
import hashlib
import logging
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

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
    visual_checks: int = 0
    last_screenshot_hash: str | None = None
    last_visual_changed: bool | None = None
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
        if result.returncode != 0:
            raise RuntimeError(f"osascript failed (exit {result.returncode}): {result.stderr.strip()}")
        return [a.strip() for a in result.stdout.strip().split(",") if a.strip()]
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Failed to hide terminal windows: {exc}") from exc


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


def _screenshot_hash(screenshot: dict[str, str]) -> str:
    data = screenshot.get("data", "")
    return hashlib.sha256(data.encode("ascii", errors="ignore")).hexdigest()


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


class CodexComputerBackend:
    """Computer Use backend that uses Codex CLI for Mac automation.

    Replaces the OpenAI Responses API visual loop with Codex-generated
    AppleScript/bash execution. Uses the ChatGPT Pro subscription (cost $0).
    """

    _SYSTEM_PROMPT = (
        "You are automating a Mac computer. "
        "Execute the task using AppleScript (osascript) or shell commands. "
        "Be direct and complete the task without asking for confirmation."
    )

    def __init__(
        self,
        cli_path: str = "codex",
        model: str = "codex-mini-latest",
        *,
        transport: Callable[[str], str] | None = None,
    ) -> None:
        self.cli_path = cli_path
        self.model = model
        self._transport = transport

    def run(self, task: str) -> str:
        if self._transport is not None:
            return self._transport(task)
        return self._run_cli(task)

    def _run_cli(self, task: str) -> str:
        resolved = shutil.which(self.cli_path)
        if not resolved:
            raise RuntimeError(
                f"Codex CLI not found at '{self.cli_path}'. "
                "Install with: npm install -g @openai/codex"
            )

        prompt = f"{self._SYSTEM_PROMPT}\n\nTask: {task}"
        cmd = [
            resolved, "exec",
            "--model", self.model,
            "--skip-git-repo-check",
            "--color", "never",
            "--", prompt,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Codex CLI not found at '{self.cli_path}'."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "Codex CLI timed out after 120s for computer use task"
            ) from exc

        if result.returncode != 0:
            raise RuntimeError(
                f"Codex computer use failed (exit {result.returncode}): {result.stderr.strip()[:500]}"
            )

        return result.stdout.strip()


class ComputerUseService:
    def __init__(
        self,
        *,
        display_width: int = 1280,
        display_height: int = 800,
        scale_factor: float = 1.0,
        action_delay: float = 0.3,
        codex_backend: "CodexComputerBackend | None" = None,
    ) -> None:
        self.display_width = display_width
        self.display_height = display_height
        self.scale_factor = scale_factor
        self.action_delay = action_delay
        self.codex_backend = codex_backend

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
        action_type = action.get("action") or action.get("type", "")
        if action_type == "screenshot":
            return self.capture_screenshot()
        # OpenAI format: click with button field
        if action_type == "click":
            x, y = self._scale_coords([action.get("x", 0), action.get("y", 0)])
            button = action.get("button", "left")
            if button == "right":
                pyautogui.rightClick(x, y)
            elif button == "middle":
                pyautogui.middleClick(x, y)
            else:
                pyautogui.click(x, y)
        elif action_type == "double_click":
            coord = action.get("coordinate") or [action.get("x", 0), action.get("y", 0)]
            x, y = self._scale_coords(coord)
            pyautogui.doubleClick(x, y)
        # Anthropic format: left_click with coordinate
        elif action_type == "left_click":
            x, y = self._scale_coords(action["coordinate"])
            pyautogui.click(x, y)
        elif action_type == "right_click":
            x, y = self._scale_coords(action["coordinate"])
            pyautogui.rightClick(x, y)
        elif action_type == "middle_click":
            x, y = self._scale_coords(action["coordinate"])
            pyautogui.middleClick(x, y)
        elif action_type == "type":
            pyautogui.typewrite(action.get("text", ""), interval=0.02)
        elif action_type in ("key", "keypress"):
            keys = action.get("text") or action.get("keys", "")
            key_list = keys.split("+") if isinstance(keys, str) else keys
            if len(key_list) > 1:
                pyautogui.hotkey(*key_list)
            else:
                pyautogui.press(key_list[0])
        elif action_type in ("mouse_move", "move"):
            x, y = self._scale_coords(
                action.get("coordinate") or [action.get("x", 0), action.get("y", 0)]
            )
            pyautogui.moveTo(x, y)
        elif action_type == "scroll":
            coord = action.get("coordinate") or [action.get("x", 0), action.get("y", 0)]
            x, y = self._scale_coords(coord)
            pyautogui.moveTo(x, y)
            direction = action.get("scroll_direction", action.get("direction", "down"))
            amount = action.get("scroll_amount", action.get("amount", 3))
            scroll_val = -amount if direction == "down" else amount
            pyautogui.scroll(scroll_val)
        elif action_type in ("left_click_drag", "drag"):
            start = action.get("start_coordinate") or [action.get("start_x", 0), action.get("start_y", 0)]
            end = action.get("coordinate") or [action.get("x", 0), action.get("y", 0)]
            sx, sy = self._scale_coords(start)
            ex, ey = self._scale_coords(end)
            pyautogui.moveTo(sx, sy)
            pyautogui.drag(ex - sx, ey - sy)
        elif action_type == "wait":
            time.sleep(action.get("ms", 1000) / 1000.0)
            return None
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
        model: str = "gpt-5.4",
        system_prompt: str | None = None,
    ) -> str:
        if self.codex_backend is not None:
            return self.codex_backend.run(session.task)
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
        tools = [{
            "type": "computer_use_preview",
            "display_width": self.display_width,
            "display_height": self.display_height,
            "environment": "mac",
        }]

        # Build initial input with screenshot
        if not session.messages:
            screenshot = self.capture_screenshot()
            session.last_screenshot_hash = _screenshot_hash(screenshot)
            screenshot_url = f"data:{screenshot['media_type']};base64,{screenshot['data']}"
            session.messages = [
                {"role": "user", "content": [
                    {"type": "input_text", "text": session.task},
                    {"type": "input_image", "image_url": screenshot_url},
                ]},
            ]
        elif session.pending_action is not None:
            # Resume after approval — send the tool output
            call_output = self._build_call_output(session.pending_action)
            session.messages.append(call_output)
            session.pending_action = None
            session.status = "running"

        previous_response_id = None

        while session.iteration < session.max_iterations:
            if session._cancelled:
                session.status = "cancelled"
                return "Session cancelled by Esc key."

            session.iteration += 1
            kwargs: dict[str, Any] = {
                "model": model,
                "tools": tools,
                "input": session.messages,
                "truncation": "auto",
            }
            if system_prompt:
                kwargs["instructions"] = system_prompt
            if previous_response_id:
                kwargs["previous_response_id"] = previous_response_id
                kwargs["input"] = session.messages[-1:] if session.messages else []

            response = client.responses.create(**kwargs)
            previous_response_id = response.id

            # Extract computer_call items and text from output
            computer_calls = [item for item in response.output if item.type == "computer_call"]
            text_items = [item for item in response.output if item.type == "text"]

            if not computer_calls:
                session.status = "done"
                return text_items[0].text if text_items else "(no response)"

            for call in computer_calls:
                if session._cancelled:
                    session.status = "cancelled"
                    return "Session cancelled by Esc key."

                action = call.action
                action_dict = action.model_dump() if hasattr(action, "model_dump") else dict(action)
                verdict = gate.classify_desktop_action(action_dict, url=session.current_url)

                if verdict.value == "needs_approval":
                    session.status = "awaiting_approval"
                    session.pending_action = {"call_id": call.call_id, **action_dict}
                    action_type = action_dict.get("type", "unknown")
                    return f"Action needs approval: {action_type} — waiting for /action_approve"

                # Execute the action
                self.execute_action(action_dict)
                # Capture new screenshot after action
                screenshot = self.capture_screenshot()
                new_hash = _screenshot_hash(screenshot)
                session.visual_checks += 1
                session.last_visual_changed = (
                    session.last_screenshot_hash is None or new_hash != session.last_screenshot_hash
                )
                session.last_screenshot_hash = new_hash
                screenshot_url = f"data:{screenshot['media_type']};base64,{screenshot['data']}"
                call_output = {
                    "type": "computer_call_output",
                    "call_id": call.call_id,
                    "output": {
                        "type": "computer_screenshot",
                        "image_url": screenshot_url,
                    },
                }
                session.messages.append(call_output)

        session.status = "done"
        return "Computer Use iteration limit reached."

    def _build_call_output(self, pending: dict[str, Any]) -> dict[str, Any]:
        """Execute a pending action and build OpenAI computer_call_output."""
        call_id = pending.pop("call_id", pending.pop("tool_use_id", "unknown"))
        result = self.execute_action(pending)
        if result is None:
            result = self.capture_screenshot()
        screenshot_url = f"data:{result['media_type']};base64,{result['data']}"
        return {
            "type": "computer_call_output",
            "call_id": call_id,
            "output": {
                "type": "computer_screenshot",
                "image_url": screenshot_url,
            },
        }

    def _scale_coords(self, coordinate: list[int]) -> tuple[int, int]:
        x = int(coordinate[0] * self.scale_factor)
        y = int(coordinate[1] * self.scale_factor)
        return x, y

    def _execute_pending_action(self, action: dict[str, Any]) -> dict[str, Any]:
        """Legacy Anthropic format — kept for compatibility with approval resume."""
        return self._build_call_output(action)


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
