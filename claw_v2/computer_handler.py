from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Callable

from claw_v2.bot_commands import BotCommand, CommandContext
from claw_v2.bot_helpers import (
    _computer_instruction_requires_actions,
    _format_computer_pending_summary,
)

logger = logging.getLogger(__name__)


class ComputerHandler:
    def __init__(
        self,
        *,
        computer: Any | None = None,
        browser_use: Any | None = None,
        computer_gate: Any | None = None,
        computer_client_factory: Callable[[], Any] | None = None,
        computer_model: str = "computer-use-preview",
        computer_system_prompt: str = "",
        approvals: Any | None = None,
        config: Any | None = None,
        observe: Any | None = None,
        capability_check: Callable[[str, str], str | None] | None = None,
        brain_handle_message: Callable[..., Any] | None = None,
    ) -> None:
        self.computer = computer
        self.browser_use = browser_use
        self.computer_gate = computer_gate
        self.computer_client_factory = computer_client_factory
        self.computer_model = computer_model
        self.computer_system_prompt = computer_system_prompt
        self.approvals = approvals
        self.config = config
        self.observe = observe
        self._check_capability = capability_check or (lambda name, fallback: None)
        self._brain_handle_message = brain_handle_message
        self._state_lock = threading.Lock()
        self._sessions: dict[str, Any] = {}
        self._client: Any | None = None

    def commands(self) -> list[BotCommand]:
        return [
            BotCommand(
                "computer",
                self.handle_command,
                exact=("/screen", "/computer"),
                prefixes=("/computer_abort", "/computer "),
            ),
            BotCommand(
                "action",
                self._handle_action_command,
                prefixes=("/action_approve ", "/action_abort "),
            ),
        ]

    def handle_command(self, context: CommandContext) -> str:
        stripped = context.stripped
        if stripped == "/screen":
            return self.screen_response()
        if stripped == "/computer":
            return "usage: /computer <instruction>"
        if stripped.startswith("/computer_abort"):
            return self.abort_response(context.session_id)
        parts = stripped.split(maxsplit=1)
        if len(parts) != 2:
            return "usage: /computer <instruction>"
        return self.computer_response(parts[1], context.session_id)

    def _handle_action_command(self, context: CommandContext) -> str:
        stripped = context.stripped
        if stripped.startswith("/action_approve "):
            parts = stripped.split()
            if len(parts) != 3:
                return "usage: /action_approve <approval_id> <token>"
            return self.action_approve_response(parts[1], parts[2])
        parts = stripped.split(maxsplit=1)
        if len(parts) != 2:
            return "usage: /action_abort <approval_id>"
        return self.action_abort_response(parts[1])

    def screen_response(self) -> str:
        degraded = self._check_capability("computer_use", "computer use unavailable")
        if degraded is not None:
            return degraded
        if self.computer is None:
            return "computer use unavailable"
        try:
            screenshot = self.computer.capture_screenshot()
        except Exception as exc:
            return f"screenshot error: {exc}"
        return json.dumps({"screenshot_data": screenshot["data"][:100] + "...", "media_type": screenshot["media_type"]})

    def computer_response(self, instruction: str, session_id: str) -> str:
        degraded = self._check_capability("computer_use", "computer use unavailable")
        if degraded is not None:
            return degraded
        if self.computer is None:
            return "computer use unavailable"
        if _computer_instruction_requires_actions(instruction):
            return self.action_response(instruction, session_id)
        try:
            screenshot = self.computer.capture_screenshot()
        except Exception as exc:
            return f"computer screenshot error: {exc}"

        content_blocks = [
            {"type": "text", "text": instruction},
            {"type": "image", "source": {"type": "base64", **screenshot}},
        ]
        memory_text = f"[Screenshot de escritorio]\n{instruction.strip()}"
        if self._brain_handle_message is None:
            return "brain not available"
        return self._brain_handle_message(
            session_id,
            content_blocks,
            memory_text=memory_text,
        ).content

    def action_response(self, instruction: str, session_id: str) -> str:
        degraded = self._check_capability("computer_control", "computer use unavailable")
        if degraded is not None:
            return degraded
        if self.browser_use is not None:
            try:
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop and loop.is_running():
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(asyncio.run, self.browser_use.run_task(instruction))
                        result = future.result(timeout=120)
                else:
                    result = asyncio.run(asyncio.wait_for(self.browser_use.run_task(instruction), timeout=120))
                return result
            except Exception as exc:
                logger.warning("browser_use fallback failed: %s", exc)

        from claw_v2.computer import ComputerSession

        with self._state_lock:
            active = self._sessions.get(session_id)
            if active is not None:
                active.status = "aborted"
            session = ComputerSession(task=instruction)
            self._sessions[session_id] = session
        return self._run_session(session_id)

    def _run_session(self, session_id: str) -> str:
        session = self._sessions.get(session_id)
        if session is None:
            return "no active computer session"
        try:
            gate = self._get_gate()
            client = None if self.computer.codex_backend is not None else self._get_client()
            result = self.computer.run_agent_loop(
                session=session,
                client=client,
                gate=gate,
                model=self.computer_model,
                system_prompt=self.computer_system_prompt,
            )
        except Exception as exc:
            self._sessions.pop(session_id, None)
            logger.exception("computer use failed for %s", session_id)
            if self.observe is not None:
                self.observe.emit("error", payload={"source": "computer_use", "error": str(exc)[:200]})
            return f"computer use error: {exc}"

        if session.status == "awaiting_approval":
            pending = dict(session.pending_action or {})
            pending_approval = self.approvals.create(
                action=pending.get("action", "computer_action"),
                summary=_format_computer_pending_summary(session.task, pending),
                metadata={
                    "kind": "computer_use",
                    "session_id": session_id,
                    "task": session.task,
                    "pending_action": pending,
                },
            )
            session.pending_action = {
                **pending,
                "approval_id": pending_approval.approval_id,
                "approval_token": pending_approval.token,
            }
            return (
                f"{result}\n\n"
                f"Approve via: `/action_approve {pending_approval.approval_id} {pending_approval.token}`\n"
                f"Abort via: `/action_abort {pending_approval.approval_id}`"
            )

        if session.status in {"done", "aborted"}:
            self._sessions.pop(session_id, None)
        return result

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self.computer_client_factory is not None:
            self._client = self.computer_client_factory()
            return self._client
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai SDK is not installed") from exc
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured for computer use")
        self._client = OpenAI(api_key=api_key)
        return self._client

    def _get_gate(self) -> Any:
        if self.computer_gate is not None:
            return self.computer_gate
        from claw_v2.computer_gate import ActionGate

        sensitive_urls = getattr(self.config, "sensitive_urls", []) if self.config is not None else []
        self.computer_gate = ActionGate(sensitive_urls=sensitive_urls)
        return self.computer_gate

    def abort_response(self, session_id: str) -> str:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return "no active computer session"
        session.status = "aborted"
        return "computer session aborted"

    def action_approve_response(self, approval_id: str, token: str) -> str:
        if self.approvals is None:
            return "approvals unavailable"
        try:
            valid = self.approvals.approve(approval_id, token)
        except FileNotFoundError:
            return f"approval {approval_id} not found"
        if not valid:
            return "invalid token"
        payload = self.approvals.read(approval_id)
        metadata = payload.get("metadata", {})
        if metadata.get("kind") != "computer_use":
            return "approved"
        session_id = metadata.get("session_id")
        if not isinstance(session_id, str):
            return "approved, but no computer session metadata was found"
        session = self._sessions.get(session_id)
        if session is None:
            return "approved, but the computer session is no longer active"
        if session.pending_action is not None and session.pending_action.get("approval_id") != approval_id:
            return "approved, but no matching pending computer action was found"
        session.status = "running"
        return self._run_session(session_id)

    def action_abort_response(self, approval_id: str) -> str:
        if self.approvals is None:
            return "approvals unavailable"
        try:
            payload = self.approvals.read(approval_id)
            self.approvals.reject(approval_id)
        except FileNotFoundError:
            return f"approval {approval_id} not found"
        metadata = payload.get("metadata", {})
        if metadata.get("kind") == "computer_use":
            session_id = metadata.get("session_id")
            if isinstance(session_id, str):
                session = self._sessions.pop(session_id, None)
                if session is not None:
                    session.status = "aborted"
            return "computer action rejected"
        return "action rejected"
