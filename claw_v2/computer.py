from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

pyautogui: Any | None = None

LOCK_PATH = Path.home() / ".claw" / "computer_use.lock"
TERMINAL_APPS = {"Terminal", "iTerm2", "Alacritty", "kitty", "Warp", "WezTerm"}


class ComputerUseUnavailable(RuntimeError):
    pass


class BrowserUsePolicyInterrupt(RuntimeError):
    """Raised when browser_use proposes an action that needs explicit approval."""

    def __init__(
        self,
        *,
        action_name: str,
        params: dict[str, Any],
        url: str | None,
        risk: str,
        approved_domains: list[str] | None = None,
    ) -> None:
        super().__init__(f"browser_use action requires approval: {action_name}")
        self.action_name = action_name
        self.params = params
        self.url = url
        self.risk = risk
        self.approved_domains = list(approved_domains or [])


def _load_pyautogui() -> Any:
    global pyautogui
    if pyautogui is not None:
        return pyautogui
    try:
        import pyautogui as imported_pyautogui
    except Exception as exc:
        raise ComputerUseUnavailable(str(exc)) from exc
    imported_pyautogui.FAILSAFE = True
    imported_pyautogui.PAUSE = 0.1
    pyautogui = imported_pyautogui
    return imported_pyautogui


@contextmanager
def _preserve_browser_use_import_env():
    """browser_use imports load .env and mutate os.environ; keep Claw's env stable."""
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


@contextmanager
def _suppress_anthropic_api_key(active: bool):
    """Force the Claude OAuth (Max subscription) path by hiding ANTHROPIC_API_KEY.

    browser_use's ``ChatAnthropic`` builds ``AsyncAnthropic(api_key=None, ...)``,
    and the Anthropic SDK falls back to ``ANTHROPIC_API_KEY`` from the environment
    when ``api_key`` is None — so a stray metered key would silently bill credits
    instead of the subscription. Browser_use runs are serialized (CDP/profile +
    browser_use locks) and the brain lane runs in subscription mode without this
    var, so popping it for the duration is safe. No-op when ``active`` is False.
    """
    if not active or "ANTHROPIC_API_KEY" not in os.environ:
        yield
        return
    saved = os.environ.pop("ANTHROPIC_API_KEY")
    try:
        yield
    finally:
        os.environ["ANTHROPIC_API_KEY"] = saved


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
    previous_response_id: str | None = None
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
        "Only execute after Claw has already completed its external approval gate. "
        "Be direct and complete the approved task without asking for extra confirmation."
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
                timeout=300,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Codex CLI not found at '{self.cli_path}'."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "Codex CLI timed out after 300s for computer use task"
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
        pag = _load_pyautogui()
        # OpenAI format: click with button field
        if action_type == "click":
            x, y = self._scale_coords([action.get("x", 0), action.get("y", 0)])
            button = action.get("button", "left")
            if button == "right":
                pag.rightClick(x, y)
            elif button == "middle":
                pag.middleClick(x, y)
            else:
                pag.click(x, y)
        elif action_type == "double_click":
            coord = action.get("coordinate") or [action.get("x", 0), action.get("y", 0)]
            x, y = self._scale_coords(coord)
            pag.doubleClick(x, y)
        # Anthropic format: left_click with coordinate
        elif action_type == "left_click":
            x, y = self._scale_coords(action["coordinate"])
            pag.click(x, y)
        elif action_type == "right_click":
            x, y = self._scale_coords(action["coordinate"])
            pag.rightClick(x, y)
        elif action_type == "middle_click":
            x, y = self._scale_coords(action["coordinate"])
            pag.middleClick(x, y)
        elif action_type == "type":
            pag.typewrite(action.get("text", ""), interval=0.02)
        elif action_type in ("key", "keypress"):
            keys = action.get("text") or action.get("keys", "")
            key_list = keys.split("+") if isinstance(keys, str) else keys
            if len(key_list) > 1:
                pag.hotkey(*key_list)
            else:
                pag.press(key_list[0])
        elif action_type in ("mouse_move", "move"):
            x, y = self._scale_coords(
                action.get("coordinate") or [action.get("x", 0), action.get("y", 0)]
            )
            pag.moveTo(x, y)
        elif action_type == "scroll":
            coord = action.get("coordinate") or [action.get("x", 0), action.get("y", 0)]
            x, y = self._scale_coords(coord)
            pag.moveTo(x, y)
            direction = action.get("scroll_direction", action.get("direction", "down"))
            amount = action.get("scroll_amount", action.get("amount", 3))
            scroll_val = -amount if direction == "down" else amount
            pag.scroll(scroll_val)
        elif action_type in ("left_click_drag", "drag"):
            start = action.get("start_coordinate") or [action.get("start_x", 0), action.get("start_y", 0)]
            end = action.get("coordinate") or [action.get("x", 0), action.get("y", 0)]
            sx, sy = self._scale_coords(start)
            ex, ey = self._scale_coords(end)
            pag.moveTo(sx, sy)
            pag.drag(ex - sx, ey - sy)
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
        current_url_resolver: Callable[[], str | None] | None = None,
    ) -> str:
        if self.codex_backend is not None:
            return self._run_codex_agent_loop(session)
        esc_listener = _EscListener(session)
        esc_listener.start()

        try:
            with _computer_use_lock():
                return self._run_loop(
                    session=session, client=client, gate=gate,
                    model=model, system_prompt=system_prompt,
                    current_url_resolver=current_url_resolver,
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
        current_url_resolver: Callable[[], str | None] | None,
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

        previous_response_id = session.previous_response_id

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
            session.previous_response_id = response.id

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
                if current_url_resolver is not None:
                    try:
                        resolved_url = current_url_resolver()
                    except Exception:
                        logger.debug("computer current_url resolver failed", exc_info=True)
                    else:
                        if isinstance(resolved_url, str) and resolved_url.strip():
                            session.current_url = resolved_url.strip()
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

    def _run_codex_agent_loop(self, session: ComputerSession) -> str:
        """Run the Codex backend only after the outer approval flow resumes it."""
        pending = dict(session.pending_action or {})
        approved = (
            pending.get("action") == "codex_computer_task"
            and pending.get("approved") is True
            and isinstance(pending.get("approval_id"), str)
        )
        if not approved:
            session.status = "awaiting_approval"
            session.pending_action = {
                "action": "codex_computer_task",
                "backend": "codex",
                "task": session.task,
            }
            return "Codex computer backend needs approval before executing local desktop automation."

        esc_listener = _EscListener(session)
        esc_listener.start()
        try:
            with _computer_use_lock():
                if session._cancelled:
                    session.status = "cancelled"
                    return "Session cancelled by Esc key."
                result = self.codex_backend.run(session.task)
        finally:
            esc_listener.stop()
        session.pending_action = None
        session.status = "done"
        return result

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


# Vision model browser_use drives the page with. Default to Claude (rides
# Hector's Max subscription via the keychain OAuth token — NO metered API),
# overridable via CLAW_BROWSER_USE_MODEL. A "claude*" model authenticates with
# the OAuth bearer; any other (e.g. gpt-*) falls back to the metered OPENAI key.
DEFAULT_BROWSER_USE_MODEL = "claude-sonnet-4-6"
# Secondary model wired as browser_use's fallback_llm when the primary is a
# Claude model — same OAuth/subscription path, so a primary-model hiccup
# (overload/availability) doesn't kill the whole browser task.
BROWSER_USE_OAUTH_FALLBACK_MODEL = "claude-haiku-4-5"
# Beta header the Anthropic API requires when authenticating /v1/messages with
# a Claude Code (Max subscription) OAuth bearer token instead of an API key.
_ANTHROPIC_OAUTH_BETA_HEADER = "oauth-2025-04-20"
# Upper bound for the best-effort post-task screenshot so a hung capture can
# never delay or fail an otherwise-completed browser task.
_BROWSER_USE_CAPTURE_TIMEOUT_SECONDS = 30


class BrowserUseService:
    """High-level browser automation via browser-use, complementing ComputerUseService.

    Use this for web tasks (navigate, click by element index, extract data).
    Use ComputerUseService for native desktop apps.
    """

    def __init__(
        self,
        *,
        cdp_url: str | None = "http://localhost:9250",
        headless: bool = True,
    ) -> None:
        self.cdp_url = cdp_url
        self.headless = headless
        # Path of the screenshot captured at the end of the most recent
        # run_task, so the caller can surface a fresh artifact (e.g. the
        # generated image) instead of a stale one. Thread-local because
        # BrowserUseService is a shared singleton and concurrent sessions each
        # run run_task in their own worker thread — a plain attribute would let
        # one session read another session's screenshot.
        self._artifact_local = threading.local()

    @property
    def last_artifact_path(self) -> str | None:
        return getattr(self._artifact_local, "path", None)

    @last_artifact_path.setter
    def last_artifact_path(self, value: str | None) -> None:
        self._artifact_local.path = value

    def _build_browser_llm(self, model: str) -> tuple[Any, Any | None]:
        """Build the browser_use planning LLM (primary, fallback_llm).

        ``claude*`` models ride Hector's Max subscription: authenticate with the
        keychain OAuth bearer + the ``oauth-2025-04-20`` beta header (NO metered
        ANTHROPIC_API_KEY), and wire a secondary Claude model as ``fallback_llm``
        so a primary overload doesn't kill the task. Any other model (gpt-*) uses
        the metered OpenAI key. Raises if a Claude model is requested but no
        subscription token is available — fail loud, never silently no-op.
        """
        with _preserve_browser_use_import_env():
            from browser_use import ChatAnthropic, ChatOpenAI

        if str(model).lower().startswith("claude"):
            token = _resolve_claude_oauth_token()
            if not token:
                raise RuntimeError(
                    "No pude resolver el token OAuth de Claude (Max) del keychain "
                    "'Claude Code-credentials'; el agente de navegador no puede "
                    "autenticar contra Anthropic por suscripción."
                )
            headers = {"anthropic-beta": _ANTHROPIC_OAUTH_BETA_HEADER}
            primary = ChatAnthropic(model=model, auth_token=token, default_headers=headers)
            fallback = None
            if BROWSER_USE_OAUTH_FALLBACK_MODEL and BROWSER_USE_OAUTH_FALLBACK_MODEL != model:
                fallback = ChatAnthropic(
                    model=BROWSER_USE_OAUTH_FALLBACK_MODEL,
                    auth_token=token,
                    default_headers=headers,
                )
            return primary, fallback
        return ChatOpenAI(model=model, api_key=os.environ.get("OPENAI_API_KEY")), None

    async def run_task(
        self,
        task: str,
        *,
        model: str = DEFAULT_BROWSER_USE_MODEL,
        max_actions_per_step: int = 5,
        use_vision: bool = True,
        save_conversation: str | None = None,
        artifact_dir: str | Path | None = None,
        timeout: float | None = None,
        action_gate: Any | None = None,
        sensitive_urls: list[str] | None = None,
        allowed_domains: list[str] | None = None,
        prohibited_domains: list[str] | None = None,
        allow_high_risk_actions: bool = False,
    ) -> str:
        with _preserve_browser_use_import_env():
            from browser_use import Agent, BrowserSession

        self.last_artifact_path = None
        normalized_allowed = _normalize_domain_patterns(allowed_domains)
        normalized_prohibited = _normalize_domain_patterns(prohibited_domains or sensitive_urls)
        browser = BrowserSession(
            cdp_url=self.cdp_url,
            headless=self.headless,
            allowed_domains=normalized_allowed or None,
            prohibited_domains=normalized_prohibited or None,
            keep_alive=True,
        )
        llm, fallback_llm = self._build_browser_llm(model)
        tools = None
        policy_interrupt: BrowserUsePolicyInterrupt | None = None
        if action_gate is not None:
            tools, policy_state = self._guarded_browser_tools(
                action_gate=action_gate,
                approved_domains=normalized_allowed,
                allow_high_risk_actions=allow_high_risk_actions,
            )

            async def _should_stop() -> bool:
                return bool(policy_state.get("should_stop"))

        else:
            policy_state = {}
            _should_stop = None
        agent = Agent(
            task=task,
            llm=llm,
            fallback_llm=fallback_llm,
            browser_session=browser,
            tools=tools,
            max_actions_per_step=max_actions_per_step,
            use_vision=use_vision,
            save_conversation_path=save_conversation,
            register_should_stop_callback=_should_stop,
        )
        artifact_path: Path | None = None
        oauth_claude = str(model).lower().startswith("claude")
        try:
            # Only the agent work is bounded by the caller's timeout. The
            # artifact capture runs AFTER, with its own budget, so a task that
            # finishes near `timeout` is never turned into a timeout failure by
            # the screenshot. The OAuth-suppress guard keeps a Claude run on the
            # Max subscription even if a metered ANTHROPIC_API_KEY is in the env.
            with _suppress_anthropic_api_key(oauth_claude):
                if timeout is not None:
                    result = await asyncio.wait_for(agent.run(), timeout=timeout)
                else:
                    result = await agent.run()
            policy_interrupt = policy_state.get("interrupt") if isinstance(policy_state, dict) else None
            if policy_interrupt is not None:
                raise policy_interrupt
            artifact_path = await self._capture_page_artifact(browser, artifact_dir)
        finally:
            await browser.stop()
        final = result.final_result()
        if final:
            text = final
        else:
            last = result.last_action()
            text = str(last) if last else "(no result)"
        if artifact_path is not None:
            self.last_artifact_path = str(artifact_path)
            return f"{text}\n\n[Captura guardada: {artifact_path}]"
        return text

    def _guarded_browser_tools(
        self,
        *,
        action_gate: Any,
        approved_domains: list[str],
        allow_high_risk_actions: bool,
    ) -> tuple[Any, dict[str, Any]]:
        with _preserve_browser_use_import_env():
            from browser_use.agent.views import ActionResult
            from browser_use.tools.service import Tools

        state: dict[str, Any] = {"should_stop": False, "interrupt": None}

        class GuardedBrowserTools(Tools):
            async def act(self, action, browser_session, **kwargs):  # type: ignore[override]
                action_name, params = _browser_use_action_parts(action)
                current_url = await _browser_use_current_url(browser_session)
                risk = action_gate.risk_browser_use_action(action_name, params, url=current_url)
                risk_value = str(getattr(risk, "value", risk))
                if risk_value == "high" and not _browser_use_high_risk_allowed(
                    url=current_url,
                    params=params,
                    approved_domains=approved_domains,
                    allow_high_risk_actions=allow_high_risk_actions,
                ):
                    interrupt = BrowserUsePolicyInterrupt(
                        action_name=action_name,
                        params=params,
                        url=current_url,
                        risk=risk_value,
                        approved_domains=_browser_use_interrupt_domains(
                            current_url=current_url,
                            params=params,
                            fallback_domains=approved_domains,
                        ),
                    )
                    state["interrupt"] = interrupt
                    state["should_stop"] = True
                    return ActionResult(error=str(interrupt))
                return await super().act(action=action, browser_session=browser_session, **kwargs)

        return GuardedBrowserTools(), state

    async def _capture_page_artifact(
        self, browser: Any, artifact_dir: str | Path | None
    ) -> Path | None:
        """Best-effort screenshot of the active page after a task, saved as a
        fresh PNG via CDP. Bounded by its own timeout and tolerant of any
        failure — never aborts the task (returns None instead).

        Uses BrowserSession.take_screenshot (CDP Page.captureScreenshot), which
        works off the CDP session directly. The older get_current_page().screenshot()
        path returns None for the page on current browser_use versions."""
        try:
            directory = Path(artifact_dir) if artifact_dir else (Path.home() / ".claw" / "images")
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"browser_use_{int(time.time() * 1000)}.png"
            await asyncio.wait_for(
                browser.take_screenshot(path=str(path), full_page=True),
                timeout=_BROWSER_USE_CAPTURE_TIMEOUT_SECONDS,
            )
            return path if path.exists() else None
        except Exception:
            logger.warning("browser_use page artifact capture failed", exc_info=True)
            return None

    async def extract(
        self,
        url: str,
        prompt: str,
        *,
        model: str = DEFAULT_BROWSER_USE_MODEL,
    ) -> str:
        """Navigate to a URL and extract information using an LLM."""
        task = f"Go to {url} and extract the following: {prompt}"
        return await self.run_task(task, model=model)

    async def quick_screenshot(self, url: str, output_path: str | None = None) -> str:
        """Take a screenshot of a URL, return base64 or save to path."""
        with _preserve_browser_use_import_env():
            from browser_use import BrowserSession

        browser = BrowserSession(
            cdp_url=self.cdp_url,
            headless=self.headless,
            keep_alive=True,
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


def _normalize_domain_patterns(values: list[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip().lower()
        if not text:
            continue
        if "://" in text:
            host = urlparse(text).hostname or text
        else:
            host = text.strip("/")
        if not host:
            continue
        if host not in seen:
            seen.add(host)
            normalized.append(host)
    return normalized


def _browser_use_action_parts(action: Any) -> tuple[str, dict[str, Any]]:
    try:
        action_data = action.model_dump(exclude_unset=True)
    except TypeError:
        action_data = action.model_dump()
    except AttributeError:
        action_data = dict(action or {})
    if not isinstance(action_data, dict) or not action_data:
        return "unknown", {}
    action_name = str(next(iter(action_data.keys())))
    params = action_data.get(action_name) or {}
    return action_name, params if isinstance(params, dict) else {"value": params}


async def _browser_use_current_url(browser_session: Any) -> str | None:
    try:
        value = await browser_session.get_current_page_url()
    except Exception:
        return None
    return value if isinstance(value, str) and value.strip() else None


def _browser_use_high_risk_allowed(
    *,
    url: str | None,
    params: dict[str, Any],
    approved_domains: list[str],
    allow_high_risk_actions: bool,
) -> bool:
    if not allow_high_risk_actions:
        return False
    if not approved_domains:
        return False
    urls = [url, str(params.get("url") or "").strip() or None]
    return any(_url_matches_domains(candidate, approved_domains) for candidate in urls if candidate)


def _browser_use_interrupt_domains(
    *,
    current_url: str | None,
    params: dict[str, Any],
    fallback_domains: list[str],
) -> list[str]:
    domains = list(fallback_domains)
    for value in (current_url, str(params.get("url") or "").strip() or None):
        host = _host_from_url(value)
        if host and host not in domains:
            domains.append(host)
    return domains


def _url_matches_domains(url: str | None, domains: list[str]) -> bool:
    host = _host_from_url(url)
    if not host:
        return False
    for domain in domains:
        normalized = domain.lower().lstrip("*.").strip()
        if host == normalized or host.endswith("." + normalized):
            return True
    return False


def _host_from_url(url: str | None) -> str | None:
    if not url:
        return None
    text = str(url).strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        host = urlparse(text).hostname
    except Exception:
        return None
    return host.lower() if host else None


def _resolve_claude_oauth_token() -> str | None:
    """Return the Claude Code (Max subscription) OAuth access token, or None.

    Read fresh from the macOS keychain ("Claude Code-credentials") on every call:
    the ``claude`` CLI used by the brain lane refreshes that keychain entry, so a
    per-task read rides the live subscription without us owning refresh. Returns
    None off-macOS or when no credential is stored. Never logs the token.
    """
    import sys

    if sys.platform != "darwin":
        # The keychain ("security" CLI + "Claude Code-credentials") is macOS-only;
        # skip the subprocess work and exception handling entirely off-mac.
        return None

    import getpass
    import json as _json

    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    candidates = [
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-a", user, "-w"],
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
    ]
    for args in candidates:
        try:
            res = subprocess.run(args, capture_output=True, text=True, timeout=5)
        except Exception:
            logger.debug("keychain lookup for Claude OAuth token failed", exc_info=True)
            continue
        blob = res.stdout.strip()
        if res.returncode != 0 or not blob:
            continue
        try:
            creds = _json.loads(blob)
        except Exception:
            continue
        oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
        oauth = oauth if isinstance(oauth, dict) else (creds if isinstance(creds, dict) else {})
        token = oauth.get("accessToken") or oauth.get("access_token")
        if token:
            return str(token).strip() or None
    return None


def _resolve_api_key() -> str | None:
    """Resolve Anthropic API key from env or shell profiles."""
    import os
    import re
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
