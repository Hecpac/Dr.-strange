from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from claw_v2.browser_capability import (
    DEFAULT_CDP_PORT,
    DEFAULT_CDP_PROFILE_DIR,
    BrowserCapability,
    BrowserCapabilityError,
)
from claw_v2.browser import DevBrowserService
from claw_v2.bot_commands import BotCommand, CommandContext
from claw_v2.bot_helpers import (
    _computer_instruction_requires_actions,
    _format_computer_pending_summary,
)
from claw_v2.redaction import redact_text

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+")
BROWSER_USE_TIMEOUT_SECONDS = 180
# Extra wall-clock the worker-thread future is allowed beyond the agent timeout,
# to cover the bounded post-task screenshot capture plus browser cleanup.
BROWSER_USE_TASK_GRACE_SECONDS = 60
_NO_RESULT_SENTINEL = "(no result)"
_INSTAGRAM_OPEN_RE = re.compile(
    r"\b(?:instagram|insta|instagram\.com)\b",
    re.IGNORECASE,
)
_SIMPLE_SOCIAL_BROWSER_ACTION_RE = re.compile(
    r"\b(?:abre|abrir|open|revisa|review|check|navega|navegar|entra|entrar|perfil|feed)\b",
    re.IGNORECASE,
)


def _error_message(exc: BaseException) -> str:
    message = str(exc).strip()
    return message or exc.__class__.__name__


def _is_unverifiable_browser_result(result: str | None) -> bool:
    text = str(result or "").strip()
    if not text:
        return True
    first_line = text.splitlines()[0].strip().lower()
    return first_line == _NO_RESULT_SENTINEL


def _format_unverifiable_browser_result(session: Any) -> str:
    lines = [
        "La automatización del navegador terminó sin un resultado verificable.",
        "No marco la acción como completada. Hay que reintentar por pasos o cambiar de herramienta.",
    ]
    artifact_path = str(getattr(session, "screenshot_path", "") or "").strip()
    if artifact_path:
        lines.append(f"Captura disponible: {artifact_path}")
    return "\n".join(lines)


def _instagram_target_url(objective: str) -> str | None:
    text = str(objective or "").strip()
    if not text or _INSTAGRAM_OPEN_RE.search(text) is None:
        return None
    if _SIMPLE_SOCIAL_BROWSER_ACTION_RE.search(text) is None:
        return None
    for raw_url in _URL_RE.findall(text):
        url = raw_url.rstrip(".,;:!?)]}")
        if "instagram.com" in url.lower():
            return url
    return "https://www.instagram.com/"


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
        current_url_resolver: Callable[[str], str | None] | None = None,
        browser_capability: Any | None = None,
        capability_status_updater: Callable[..., None] | None = None,
        browser_use_factory: Callable[[str], Any] | None = None,
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
        self._current_url_resolver = current_url_resolver
        self.browser_capability = browser_capability
        self._capability_status_updater = capability_status_updater
        self._browser_use_factory = browser_use_factory
        self._state_lock = threading.Lock()
        self._sessions: dict[str, Any] = {}
        self._client: Any | None = None
        self._browser_use_lock = threading.Lock()
        self._cdp_locks_guard = threading.Lock()
        self._cdp_profile_locks: dict[str, threading.Lock] = {}

    def commands(self) -> list[BotCommand]:
        return [
            BotCommand(
                "computer",
                self.handle_command,
                exact=("/screen", "/computer", "/computer_diag", "/computer_diagnostics"),
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
        if stripped in {"/computer_diag", "/computer_diagnostics"}:
            return self.diagnostics_response(context.session_id)
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
            self._emit(
                "computer_screenshot_failed",
                {"source": "screen_command", "error": _error_message(exc)[:200]},
            )
            return f"screenshot error: {exc}"
        self._emit(
            "computer_screenshot_captured",
            {
                "source": "screen_command",
                "media_type": screenshot.get("media_type"),
                "encoded_bytes": len(str(screenshot.get("data") or "")),
            },
        )
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
            self._emit(
                "computer_screenshot_failed",
                {
                    "source": "computer_read",
                    "session_id": session_id,
                    "instruction_hash": _instruction_hash(instruction),
                    "error": _error_message(exc)[:200],
                },
            )
            return f"computer screenshot error: {exc}"
        self._emit(
            "computer_screenshot_captured",
            {
                "source": "computer_read",
                "session_id": session_id,
                "instruction_hash": _instruction_hash(instruction),
                "media_type": screenshot.get("media_type"),
                "encoded_bytes": len(str(screenshot.get("data") or "")),
            },
        )

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

        from claw_v2.computer import ComputerSession

        with self._state_lock:
            active = self._sessions.get(session_id)
            if active is not None:
                active.status = "aborted"
                self._emit(
                    "computer_session_superseded",
                    {"session_id": session_id, "previous_status": "aborted"},
                )
            current_url = self._resolve_current_url(session_id, instruction)
            session = ComputerSession(
                task=instruction,
                current_url=current_url,
            )
            if self._should_use_browser_backend(instruction, current_url=current_url):
                session.status = "awaiting_approval"
                session.pending_action = {
                    "action": "browser_use_task",
                    "backend": "browser_use",
                    "task": instruction,
                }
            self._sessions[session_id] = session
        self._emit(
            "computer_session_started",
            {
                "session_id": session_id,
                "backend": self._session_backend(session),
                "current_url": current_url,
                "instruction_hash": _instruction_hash(instruction),
                "instruction_preview": _instruction_preview(instruction),
            },
        )
        return self._run_session(session_id)

    def _run_session(self, session_id: str) -> str:
        session = self._sessions.get(session_id)
        if session is None:
            return "no active computer session"
        backend = self._session_backend(session)
        self._emit(
            "computer_backend_selected",
            {
                "session_id": session_id,
                "backend": backend,
                "status": getattr(session, "status", None),
                "current_url": getattr(session, "current_url", None),
            },
        )
        try:
            if self._is_browser_use_session(session):
                result = self._run_browser_use_session(session)
            else:
                if self.computer is None:
                    return "computer use unavailable"
                gate = self._get_gate()
                client = None if self.computer.codex_backend is not None else self._get_client()
                result = self.computer.run_agent_loop(
                    session=session,
                    client=client,
                    gate=gate,
                    model=self.computer_model,
                    system_prompt=self.computer_system_prompt,
                    current_url_resolver=lambda: self._resolve_current_url(session_id, getattr(session, "task", "")),
                )
        except Exception as exc:
            self._sessions.pop(session_id, None)
            message = _error_message(exc)
            logger.exception("computer use failed for %s", session_id)
            if backend == "browser_use" and "timed out" in message.lower():
                self._emit(
                    "computer_browser_use_timeout",
                    {
                        "session_id": session_id,
                        "backend": backend,
                        "timeout_seconds": self._browser_use_timeout(),
                        "current_url": getattr(session, "current_url", None),
                        "instruction_hash": _instruction_hash(getattr(session, "task", "")),
                    },
                )
            self._emit(
                "computer_session_failed",
                {
                    "session_id": session_id,
                    "backend": backend,
                    "status": getattr(session, "status", None),
                    "error": message[:200],
                    "instruction_hash": _instruction_hash(getattr(session, "task", "")),
                },
            )
            if self.observe is not None:
                self.observe.emit("error", payload={"source": "computer_use", "error": message[:200]})
            return f"computer use error: {message}"

        if session.status == "awaiting_approval":
            if self.approvals is None:
                session.status = "aborted"
                self._sessions.pop(session_id, None)
                self._emit(
                    "computer_approval_unavailable",
                    {"session_id": session_id, "backend": backend},
                )
                return "computer action requires approval, but approvals are unavailable"
            pending = dict(session.pending_action or {})
            screenshot_metadata = self._capture_approval_screenshot(session_id, session)
            # A screenshot backend only exists for the desktop computer service;
            # browser_use-only (or headless) deploys have self.computer=None and
            # never had screenshot binding, so they must NOT be blocked here.
            screenshot_backend_available = self.computer is not None and hasattr(
                self.computer, "capture_screenshot"
            )
            if (
                screenshot_backend_available
                and "screenshot_hash" not in screenshot_metadata
            ):
                # Fail-closed: the backend exists but capture failed, so an
                # approval would have no anti-TOCTOU visual binding
                # (_validate_pending_approval_scope silently skips the comparison
                # when the hash is absent). Refuse rather than approve blind.
                session.status = "aborted"
                self._sessions.pop(session_id, None)
                self._emit(
                    "computer_approval_blocked_no_screenshot",
                    {
                        "session_id": session_id,
                        "backend": backend,
                        "screenshot_error": screenshot_metadata.get("screenshot_error"),
                    },
                )
                return (
                    "No puedo pedir aprobación segura: no pude capturar el estado visual "
                    "actual. Reenvíame el objetivo cuando la captura de pantalla funcione."
                )
            approval_scope = {
                "backend": backend,
                "action_hash": _approval_action_hash(pending),
                "current_url": getattr(session, "current_url", None),
                "url_origin": _url_origin(getattr(session, "current_url", None)),
                "screenshot_hash": screenshot_metadata.get("screenshot_hash"),
                "approved_domains": _normalized_domains(pending.get("approved_domains")),
            }
            pending["approval_scope"] = approval_scope
            summary = _format_computer_pending_summary(session.task, pending)
            pending_approval = self.approvals.create(
                action=str(pending.get("action") or pending.get("type") or "computer_action"),
                summary=summary,
                metadata={
                    "kind": "computer_use",
                    "session_id": session_id,
                    "task": session.task,
                    "pending_action": pending,
                    "current_url": session.current_url,
                    "approval_scope": approval_scope,
                    **screenshot_metadata,
                },
            )
            session.pending_action = {
                **pending,
                "approval_id": pending_approval.approval_id,
                "approval_token": pending_approval.token,
            }
            self._emit(
                "computer_approval_pending",
                {
                    "session_id": session_id,
                    "backend": self._session_backend(session),
                    "approval_id": pending_approval.approval_id,
                    "action": str(pending.get("action") or pending.get("type") or "computer_action"),
                    "current_url": getattr(session, "current_url", None),
                    "screenshot_captured": "screenshot_path" in screenshot_metadata,
                    "screenshot_error": screenshot_metadata.get("screenshot_error"),
                },
            )
            return (
                "Necesito tu autorización para continuar con esta acción de escritorio.\n"
                f"Acción: {summary}\n"
                "Responde `te autorizo` para ejecutarla o `aborta` para cancelarla."
            )

        if session.status in {"done", "aborted"}:
            self._sessions.pop(session_id, None)
        if session.status == "done":
            self._emit(
                "computer_session_completed",
                {
                    "session_id": session_id,
                    "backend": backend,
                    "result_chars": len(str(result or "")),
                },
            )
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

    def _auto_approve_enabled(self) -> bool:
        return bool(getattr(self.config, "computer_auto_approve", False))

    def _browser_use_model(self) -> str:
        from claw_v2.computer import DEFAULT_BROWSER_USE_MODEL

        model = str(getattr(self.config, "computer_browser_use_model", "") or "").strip()
        return model or DEFAULT_BROWSER_USE_MODEL

    def _browser_use_timeout(self) -> int:
        configured = getattr(self.config, "computer_browser_use_timeout_seconds", 0)
        try:
            configured = int(configured)
        except (TypeError, ValueError):
            configured = 0
        return configured if configured > 0 else BROWSER_USE_TIMEOUT_SECONDS

    def _browser_task_is_sensitive(self, task: str | None, current_url: str | None) -> bool:
        """Best-effort: a browser_use task is sensitive if it starts on a
        sensitive URL or its instruction names a sensitive domain (by brand).
        Sensitive tasks keep the approval gate even when auto-approve is enabled.

        Note: browser_use runs an autonomous agent that can navigate elsewhere
        mid-task; this pre-check only sees the initial instruction/URL. Reliable
        protection for a sensitive site still depends on it being in
        SENSITIVE_URLS (and, ideally, per-navigation gating in browser_use)."""
        gate = self._get_gate()
        return gate.is_sensitive_url(current_url) or gate.is_sensitive_text(task)

    def _get_gate(self) -> Any:
        if self.computer_gate is not None:
            return self.computer_gate
        from claw_v2.computer_gate import ActionGate

        sensitive_urls = getattr(self.config, "sensitive_urls", []) if self.config is not None else []
        self.computer_gate = ActionGate(sensitive_urls=sensitive_urls, auto_approve=self._auto_approve_enabled())
        return self.computer_gate

    def abort_response(self, session_id: str) -> str:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return "no active computer session"
        session.status = "aborted"
        self._emit("computer_session_aborted", {"session_id": session_id})
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
        return self._resume_approved_computer_action(approval_id)

    def action_approve_internal_response(self, approval_id: str, *, session_id: str | None = None) -> str:
        if self.approvals is None:
            return "approvals unavailable"
        try:
            payload = self.approvals.read(approval_id)
        except FileNotFoundError:
            return f"approval {approval_id} not found"
        metadata = payload.get("metadata", {})
        if metadata.get("kind") == "computer_use" and session_id is not None and metadata.get("session_id") != session_id:
            return "approval does not belong to this session"
        status = str(payload.get("status") or "")
        if status == "pending":
            if not self.approvals.approve_internal(approval_id):
                return "approval could not be registered"
        elif status != "approved":
            return f"approval {approval_id} is {status}"
        return self._resume_approved_computer_action(approval_id)

    def _resume_approved_computer_action(self, approval_id: str) -> str:
        if self.approvals is None:
            return "approvals unavailable"
        payload = self.approvals.read(approval_id)
        metadata = payload.get("metadata", {})
        if metadata.get("kind") != "computer_use":
            return "approved"
        session_id = metadata.get("session_id")
        if not isinstance(session_id, str):
            self._emit("computer_approval_resume_blocked", {"approval_id": approval_id, "reason": "missing_session_id"})
            return "approved, but no computer session metadata was found"
        session = self._sessions.get(session_id)
        if session is None:
            self._emit("computer_approval_resume_blocked", {"approval_id": approval_id, "session_id": session_id, "reason": "session_not_active"})
            return "approved, but the computer session is no longer active"
        if session.pending_action is not None and session.pending_action.get("approval_id") != approval_id:
            self._emit("computer_approval_resume_blocked", {"approval_id": approval_id, "session_id": session_id, "reason": "approval_mismatch"})
            return "approved, but no matching pending computer action was found"
        scope_error = self._validate_pending_approval_scope(session_id, session, metadata)
        if scope_error is not None:
            self._emit(
                "computer_approval_resume_blocked",
                {"approval_id": approval_id, "session_id": session_id, "reason": scope_error},
            )
            # Drop the now-stale session so it doesn't linger in awaiting_approval
            # forever (orphan/leak); the user is told to re-send the objective.
            self._sessions.pop(session_id, None)
            return "Aprobación registrada, pero el contexto de computer cambió. Reenvíame el objetivo para generar una aprobación nueva."
        if session.pending_action is not None:
            session.pending_action["approved"] = True
        session.status = "running"
        self._emit(
            "computer_approval_resume_started",
            {
                "approval_id": approval_id,
                "session_id": session_id,
                "backend": self._session_backend(session),
            },
        )
        return self._run_session(session_id)

    def _validate_pending_approval_scope(self, session_id: str, session: Any, metadata: dict[str, Any]) -> str | None:
        pending = dict(session.pending_action or {})
        scope = metadata.get("approval_scope") if isinstance(metadata, dict) else None
        if not isinstance(scope, dict):
            scope = pending.get("approval_scope")
        if not isinstance(scope, dict):
            return None
        expected_hash = str(scope.get("action_hash") or "")
        if expected_hash and _approval_action_hash(pending) != expected_hash:
            return "action_hash_changed"
        expected_origin = str(scope.get("url_origin") or "")
        if expected_origin:
            current_url = self._resolve_current_url(session_id, getattr(session, "task", "")) or getattr(session, "current_url", None)
            if isinstance(current_url, str) and current_url.strip():
                session.current_url = current_url.strip()
            current_origin = _url_origin(getattr(session, "current_url", None))
            if current_origin != expected_origin:
                return "url_origin_changed"
        expected_screenshot_hash = str(scope.get("screenshot_hash") or "")
        if expected_screenshot_hash:
            current_screenshot_hash = self._current_screenshot_hash()
            if not current_screenshot_hash:
                return "screenshot_unavailable"
            if current_screenshot_hash != expected_screenshot_hash:
                return "screenshot_changed"
        return None

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

    def _resolve_current_url(self, session_id: str, instruction: str) -> str | None:
        match = _URL_RE.search(instruction)
        if match is not None:
            return match.group(0).rstrip(".,;:!?")
        if self._current_url_resolver is None:
            return None
        try:
            value = self._current_url_resolver(session_id)
        except Exception:
            logger.debug("current_url resolver failed for computer session %s", session_id, exc_info=True)
            return None
        return value if isinstance(value, str) and value.strip() else None

    def _should_use_browser_backend(self, instruction: str, *, current_url: str | None) -> bool:
        if self.browser_use is None:
            return False
        normalized = instruction.lower()
        if any(token in normalized for token in ("chatgpt", "chat.openai.com", "chrome/cdp", "browser")):
            return True
        if _URL_RE.search(instruction) is not None:
            return True
        return bool(current_url and current_url.startswith(("http://", "https://")))

    @staticmethod
    def _is_browser_use_session(session: Any) -> bool:
        pending = session.pending_action if isinstance(session.pending_action, dict) else {}
        return pending.get("action") == "browser_use_task" or pending.get("backend") == "browser_use"

    def _run_browser_use_session(self, session: Any) -> str:
        pending = dict(session.pending_action or {})
        approved = (
            pending.get("action") == "browser_use_task"
            and pending.get("approved") is True
            and isinstance(pending.get("approval_id"), str)
        )
        explicitly_approved = approved
        if not approved and self._auto_approve_enabled() and not self._browser_task_is_sensitive(
            session.task, getattr(session, "current_url", None)
        ):
            approved = True
            self._emit(
                "computer_browser_use_auto_approved",
                {
                    "backend": "browser_use",
                    "current_url": getattr(session, "current_url", None),
                    "instruction_hash": _instruction_hash(getattr(session, "task", "")),
                },
            )
        if not approved:
            session.status = "awaiting_approval"
            session.pending_action = {
                "action": "browser_use_task",
                "backend": "browser_use",
                "task": session.task,
                "approved_domains": _domains_for_browser_task(
                    session.task,
                    getattr(session, "current_url", None),
                    sensitive_urls=list(getattr(self.config, "sensitive_urls", []) or []),
                ),
            }
            self._emit(
                "computer_browser_use_approval_required",
                {
                    "backend": "browser_use",
                    "current_url": getattr(session, "current_url", None),
                    "instruction_hash": _instruction_hash(getattr(session, "task", "")),
                },
            )
            return "Browser automation needs approval before executing authenticated browser actions."
        if self.browser_use is None:
            raise RuntimeError("browser_use unavailable for approved browser automation")
        self._emit(
            "computer_browser_use_task_started",
            {
                "backend": "browser_use",
                "timeout_seconds": self._browser_use_timeout(),
                "current_url": getattr(session, "current_url", None),
                "instruction_hash": _instruction_hash(getattr(session, "task", "")),
            },
        )
        try:
            result = self._run_browser_use_task(
                session,
                allow_high_risk_actions=explicitly_approved,
                approved_domains=_normalized_domains(
                    pending.get("approved_domains")
                    or _domains_for_browser_task(
                        session.task,
                        getattr(session, "current_url", None),
                        sensitive_urls=list(getattr(self.config, "sensitive_urls", []) or []),
                    )
                ),
            )
        except Exception as exc:
            from claw_v2.computer import BrowserUsePolicyInterrupt

            if not isinstance(exc, BrowserUsePolicyInterrupt):
                raise
            session.status = "awaiting_approval"
            session.pending_action = {
                "action": "browser_use_task",
                "backend": "browser_use",
                "task": session.task,
                "interrupted_action": {
                    "action": exc.action_name,
                    "params": exc.params,
                    "url": exc.url,
                    "risk": exc.risk,
                },
                "approved_domains": exc.approved_domains
                or _domains_for_browser_task(
                    session.task,
                    exc.url,
                    sensitive_urls=list(getattr(self.config, "sensitive_urls", []) or []),
                ),
            }
            self._emit(
                "computer_browser_use_policy_interrupted",
                {
                    "backend": "browser_use",
                    "action": exc.action_name,
                    "risk": exc.risk,
                    "current_url": exc.url,
                    "instruction_hash": _instruction_hash(getattr(session, "task", "")),
                },
            )
            return "Browser automation needs approval before continuing with a high-risk browser action."
        artifact_path = getattr(session, "screenshot_path", None)
        session.pending_action = None
        session.status = "done"
        if _is_unverifiable_browser_result(result):
            self._emit(
                "computer_browser_use_task_unverifiable_result",
                {
                    "backend": "browser_use",
                    "artifact_saved": bool(artifact_path),
                    "instruction_hash": _instruction_hash(getattr(session, "task", "")),
                },
            )
            return _format_unverifiable_browser_result(session)
        self._emit(
            "computer_browser_use_task_succeeded",
            {
                "backend": "browser_use",
                "result_chars": len(str(result or "")),
                "artifact_saved": bool(artifact_path),
                "instruction_hash": _instruction_hash(getattr(session, "task", "")),
            },
        )
        return result

    def _run_browser_use_task(
        self,
        session: Any,
        *,
        allow_high_risk_actions: bool = False,
        approved_domains: list[str] | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        import asyncio

        model = self._browser_use_model()
        # Delegated browse jobs pass the long browser/CDP budget; interactive
        # sessions fall back to the configured (short) default.
        timeout = (
            int(timeout_seconds)
            if timeout_seconds and timeout_seconds > 0
            else self._browser_use_timeout()
        )
        timeout_message = (
            f"browser_use timed out after {timeout}s while executing approved browser automation"
        )

        async def _run() -> Any:
            try:
                # run_task bounds only the agent work by `timeout`; the
                # best-effort artifact capture runs afterwards on its own budget.
                result = await self.browser_use.run_task(
                    session.task,
                    model=model,
                    timeout=timeout,
                    action_gate=self._get_gate(),
                    sensitive_urls=list(getattr(self.config, "sensitive_urls", []) or []),
                    allowed_domains=approved_domains if allow_high_risk_actions else None,
                    prohibited_domains=None if allow_high_risk_actions else list(getattr(self.config, "sensitive_urls", []) or []),
                    allow_high_risk_actions=allow_high_risk_actions,
                    max_actions_per_step=1,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(timeout_message) from exc
            # Bind the artifact to THIS session inside the worker thread, where
            # the thread-local last_artifact_path was just set — avoids the
            # shared-state race across concurrent sessions.
            session.screenshot_path = getattr(self.browser_use, "last_artifact_path", None)
            return result

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            import concurrent.futures

            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = pool.submit(lambda: asyncio.run(_run()))
            try:
                return str(future.result(timeout=timeout + BROWSER_USE_TASK_GRACE_SECONDS))
            except concurrent.futures.TimeoutError as exc:
                future.cancel()
                raise RuntimeError(timeout_message) from exc
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        return str(asyncio.run(_run()))

    def run_delegated_browser_task(
        self, objective: str, *, task_id: str | None = None, mode: str | None = None
    ) -> str:
        """One-shot browser task for a delegated CDP/browse job (option b, 2026-06-13).

        Runs the autonomous browser-use agent in-process (Playwright in the daemon
        venv) so it reaches Chrome CDP directly — unlike a Codex coordinator
        worker, whose ``--sandbox workspace-write`` denies network and cannot
        connect to localhost:9250. Wired as ``TaskHandler.browser_executor``;
        honors the same action gate / sensitive-URL policy as interactive
        browser_use, with the long browser/CDP timeout. May raise on timeout —
        the TaskHandler branch contains executor failures.
        """
        import types as _types

        from claw_v2.bot_helpers import _LONG_BROWSER_OPERATION_TIMEOUT_SECONDS

        cdp_port = self._delegated_browser_cdp_port()
        cdp_profile = f"http://127.0.0.1:{cdp_port}"
        lock = self._cdp_profile_lock(cdp_profile)
        if not lock.acquire(blocking=False):
            self._emit(
                "delegated_browser_task_queued",
                {"task_id": task_id, "mode": mode, "cdp_profile": cdp_profile},
            )
            lock.acquire()
        try:
            try:
                cdp_endpoint = self._get_browser_capability().ensure_ready(
                    port=cdp_port,
                    profile_dir=DEFAULT_CDP_PROFILE_DIR,
                )
            except BrowserCapabilityError as exc:
                return f"No pude conectar al navegador (CDP): {exc}"
            self._mark_capability_available("chrome_cdp")
            # Named-profile health gate (X-first): a cheap, deterministic CDP
            # probe BEFORE spending the LLM. If the session is logged out or
            # behind an anti-bot challenge, return a clear human state and do NOT
            # attempt evasion. Generic browse → gate returns None → normal path.
            gate = self._browser_profile_gate(objective, cdp_endpoint, task_id=task_id)
            if gate is not None:
                return gate
            deterministic = self._run_deterministic_social_browser_task(
                objective,
                cdp_endpoint=cdp_endpoint,
                task_id=task_id,
                mode=mode,
            )
            if deterministic is not None:
                return deterministic
            with self._browser_use_lock:
                self._ensure_browser_use_service(cdp_endpoint)
                self._set_browser_use_cdp_url(cdp_endpoint)
                if self.browser_use is None:
                    return (
                        "No puedo ejecutar la tarea de navegador: browser_use no está "
                        "disponible en este runtime."
                    )
                self._mark_capability_available("browser_use")
                self._emit(
                    "delegated_browser_task_started",
                    {"task_id": task_id, "mode": mode, "objective": objective[:200]},
                )
                session = _types.SimpleNamespace(task=objective, screenshot_path=None)
                return self._run_browser_use_task(
                    session, timeout_seconds=_LONG_BROWSER_OPERATION_TIMEOUT_SECONDS
                )
        finally:
            lock.release()

    def _run_deterministic_social_browser_task(
        self,
        objective: str,
        *,
        cdp_endpoint: str,
        task_id: str | None,
        mode: str | None,
    ) -> str | None:
        target_url = _instagram_target_url(objective)
        if target_url is None:
            return None
        self._emit(
            "deterministic_browser_task_started",
            {"task_id": task_id, "mode": mode, "target_url": target_url},
        )
        try:
            browser = DevBrowserService()
            result = browser.chrome_navigate(
                target_url,
                cdp_url=cdp_endpoint,
                page_url_pattern="instagram.com",
            )
            screenshot = browser.chrome_screenshot(
                cdp_url=cdp_endpoint,
                page_url_pattern="instagram.com",
                name="instagram-open.png",
            )
        except Exception as exc:
            message = _error_message(exc)
            self._emit(
                "deterministic_browser_task_failed",
                {"task_id": task_id, "mode": mode, "target_url": target_url, "error": message},
            )
            return f"No pude completar la tarea de navegador determinística: {message}"

        final_url = str(getattr(result, "url", "") or getattr(screenshot, "url", "") or target_url)
        title = str(getattr(result, "title", "") or getattr(screenshot, "title", "") or "").strip()
        screenshot_path = str(getattr(screenshot, "screenshot_path", "") or "").strip()
        content = str(getattr(result, "content", "") or getattr(screenshot, "content", "") or "").strip()
        content_hint = ""
        if content:
            compact = " ".join(content.split())
            content_hint = f"\nTexto visible: {compact[:240]}"
        self._emit(
            "deterministic_browser_task_completed",
            {
                "task_id": task_id,
                "mode": mode,
                "target_url": target_url,
                "final_url": final_url,
                "title": title,
                "screenshot_path": screenshot_path,
            },
        )
        lines = [
            "Instagram abierto en Chrome CDP.",
            f"URL final: {final_url}",
        ]
        if title:
            lines.append(f"Título: {title}")
        if screenshot_path:
            lines.append(f"Captura guardada: {screenshot_path}")
        if content_hint:
            lines.append(content_hint.strip())
        return "\n".join(lines)

    def _browser_profile_gate(
        self, objective: str, cdp_endpoint: str, *, task_id: str | None
    ) -> str | None:
        """Run the named-profile login/challenge health check (X-first).

        Returns a human-facing message when the task must NOT run (logged out or
        anti-bot challenge), else None to proceed. ``ok`` and ``unavailable``
        both proceed — we never block real work on a flaky probe, only on a
        confirmed bad state.
        """
        from claw_v2.browser_profiles import (
            BrowserProfileHealth,
            check_profile_health,
            human_message,
            resolve_profile_for_objective,
        )

        profile = resolve_profile_for_objective(objective)
        if profile is None:
            return None
        health, detail = check_profile_health(profile, cdp_endpoint)
        self._emit(
            "browser_profile_health_checked",
            {"task_id": task_id, "profile": profile.name, "health": health.value},
        )
        if health is BrowserProfileHealth.NEEDS_LOGIN:
            self._emit(
                "browser_profile_needs_login",
                {"task_id": task_id, "profile": profile.name},
            )
            return human_message(profile, health, detail)
        if health is BrowserProfileHealth.BLOCKED_BY_CHALLENGE:
            self._emit(
                "browser_profile_blocked_by_challenge",
                {"task_id": task_id, "profile": profile.name},
            )
            return human_message(profile, health, detail)
        return None

    def _delegated_browser_cdp_port(self) -> int:
        cdp_url = str(getattr(self.browser_use, "cdp_url", "") or "").strip()
        if cdp_url:
            try:
                parsed = urlparse(cdp_url)
                if parsed.port is not None:
                    return parsed.port
            except ValueError:
                logger.debug("invalid browser_use cdp_url ignored: %s", cdp_url)
        configured = getattr(self.config, "claw_chrome_port", DEFAULT_CDP_PORT)
        try:
            return int(configured)
        except (TypeError, ValueError):
            return DEFAULT_CDP_PORT

    def _get_browser_capability(self) -> Any:
        if self.browser_capability is None:
            self.browser_capability = BrowserCapability(observe=self.observe)
        return self.browser_capability

    def _ensure_browser_use_service(self, endpoint: str) -> None:
        if self.browser_use is None:
            if self._browser_use_factory is not None:
                self.browser_use = self._browser_use_factory(endpoint)
            else:
                from claw_v2.computer import BrowserUseService

                self.browser_use = BrowserUseService(cdp_url=endpoint)

    def _set_browser_use_cdp_url(self, endpoint: str) -> None:
        if self.browser_use is None:
            return
        try:
            setattr(self.browser_use, "cdp_url", endpoint)
        except Exception:
            logger.debug("could not update browser_use cdp_url", exc_info=True)

    def _mark_capability_available(self, name: str) -> None:
        if self._capability_status_updater is None:
            return
        try:
            self._capability_status_updater(name, available=True, reason=None)
        except Exception:
            logger.debug("capability status update failed for %s", name, exc_info=True)

    def _cdp_profile_lock(self, cdp_profile: str) -> threading.Lock:
        with self._cdp_locks_guard:
            lock = self._cdp_profile_locks.get(cdp_profile)
            if lock is None:
                lock = threading.Lock()
                self._cdp_profile_locks[cdp_profile] = lock
            return lock

    def _capture_approval_screenshot(self, session_id: str, session: Any) -> dict[str, str]:
        if self.computer is None or not hasattr(self.computer, "capture_screenshot"):
            return {}
        try:
            screenshot = self.computer.capture_screenshot()
            encoded = screenshot.get("data", "")
            media_type = str(screenshot.get("media_type") or "image/png")
            raw = base64.b64decode(encoded)
            root = Path(getattr(self.approvals, "root", None) or getattr(self.config, "approvals_root", ""))
            if not str(root):
                root = Path.home() / ".claw" / "pending_approvals"
            target_dir = root / "computer_screenshots"
            target_dir.mkdir(parents=True, exist_ok=True)
            safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id)[:80] or "session"
            suffix = ".png" if media_type == "image/png" else ".bin"
            path = target_dir / f"{safe_session}-{int(time.time() * 1000)}{suffix}"
            path.write_bytes(raw)
            session.screenshot_path = str(path)
            self._emit(
                "computer_approval_screenshot_captured",
                {
                    "session_id": session_id,
                    "media_type": media_type,
                    "bytes": len(raw),
                },
            )
            return {
                "screenshot_path": str(path),
                "screenshot_media_type": media_type,
                "screenshot_hash": hashlib.sha256(raw).hexdigest(),
            }
        except Exception as exc:
            logger.warning("approval screenshot capture failed for %s: %s", session_id, exc)
            self._emit(
                "computer_approval_screenshot_failed",
                {"session_id": session_id, "error": _error_message(exc)[:200]},
            )
            return {"screenshot_error": str(exc)[:200]}

    def _current_screenshot_hash(self) -> str | None:
        if self.computer is None or not hasattr(self.computer, "capture_screenshot"):
            return None
        try:
            screenshot = self.computer.capture_screenshot()
            encoded = str(screenshot.get("data") or "")
            try:
                raw = base64.b64decode(encoded)
            except Exception:
                raw = encoded.encode("utf-8", errors="ignore")
            return hashlib.sha256(raw).hexdigest()
        except Exception:
            logger.debug("current screenshot hash capture failed", exc_info=True)
            return None

    def diagnostics_response(self, session_id: str) -> str:
        self._emit("computer_diagnostic_started", {"session_id": session_id})
        checks: list[tuple[str, str, str]] = []
        backend = self._configured_backend()

        use_degraded = self._check_capability("computer_use", "computer use unavailable")
        control_degraded = self._check_capability("computer_control", "computer control unavailable")
        checks.append(("computer_use", "degraded" if use_degraded else "ok", use_degraded or "available"))
        checks.append(("computer_control", "degraded" if control_degraded else "ok", control_degraded or "available"))
        checks.append(("backend", "ok" if backend != "unavailable" else "degraded", backend))

        checks.append(self._diagnose_pyautogui_display())
        checks.append(self._diagnose_screenshot())
        checks.append(self._diagnose_browser_use())
        checks.append(self._diagnose_model_credentials(backend))

        status = "ok" if all(item[1] == "ok" for item in checks) else "degraded"
        payload = {
            "session_id": session_id,
            "status": status,
            "backend": backend,
            "checks": [
                {"name": name, "status": check_status, "detail": detail}
                for name, check_status, detail in checks
            ],
        }
        self._emit("computer_diagnostic_result", payload)

        lines = [f"Diagnostico Computer Use: {status}", f"Backend configurado: {backend}", "Checks:"]
        for name, check_status, detail in checks:
            lines.append(f"- {name}: {check_status} - {detail}")
        return "\n".join(lines)

    def _diagnose_pyautogui_display(self) -> tuple[str, str, str]:
        try:
            import pyautogui

            size = pyautogui.size()
            width = int(getattr(size, "width", 0))
            height = int(getattr(size, "height", 0))
        except Exception as exc:
            return ("pyautogui_display", "degraded", _error_message(exc)[:160])
        if width <= 0 or height <= 0:
            return ("pyautogui_display", "degraded", f"{width}x{height}")
        return ("pyautogui_display", "ok", f"{width}x{height}")

    def _diagnose_screenshot(self) -> tuple[str, str, str]:
        if self.computer is None or not hasattr(self.computer, "capture_screenshot"):
            return ("screenshot", "degraded", "computer service unavailable")
        try:
            try:
                screenshot = self.computer.capture_screenshot(exclude_terminals=False)
            except TypeError:
                screenshot = self.computer.capture_screenshot()
            encoded = str(screenshot.get("data") or "")
            media_type = str(screenshot.get("media_type") or "unknown")
            if not encoded:
                return ("screenshot", "degraded", f"empty screenshot data; media_type={media_type}")
            return ("screenshot", "ok", f"{media_type}; encoded_bytes={len(encoded)}")
        except Exception as exc:
            return ("screenshot", "degraded", _error_message(exc)[:160])

    def _diagnose_browser_use(self) -> tuple[str, str, str]:
        if self.browser_use is None:
            return ("browser_use", "degraded", "browser_use service unavailable")
        cdp_url = str(getattr(self.browser_use, "cdp_url", "") or "").rstrip("/")
        if not cdp_url:
            return ("browser_use", "degraded", "missing cdp_url")
        try:
            with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=2) as response:
                raw = response.read(8192)
            data = json.loads(raw.decode("utf-8"))
            browser = str(data.get("Browser") or "unknown")
            user_agent = str(data.get("User-Agent") or "")
            headless = "headless" in user_agent.lower()
            detail = f"{browser}; headless={headless}; cdp={cdp_url}"
            return ("browser_use_cdp", "ok", detail)
        except Exception as exc:
            return ("browser_use_cdp", "degraded", f"{cdp_url}: {_error_message(exc)[:120]}")

    def _diagnose_model_credentials(self, backend: str) -> tuple[str, str, str]:
        if backend == "openai":
            return (
                "openai_api_key",
                "ok" if bool(os.getenv("OPENAI_API_KEY")) else "degraded",
                "configured" if bool(os.getenv("OPENAI_API_KEY")) else "missing",
            )
        if backend == "codex":
            codex_backend = getattr(self.computer, "codex_backend", None)
            cli_path = str(getattr(codex_backend, "cli_path", "") or "codex")
            return ("codex_cli", "ok", cli_path)
        return ("model_credentials", "degraded", "backend unavailable")

    def _configured_backend(self) -> str:
        if self.computer is None:
            return "unavailable"
        if getattr(self.computer, "codex_backend", None) is not None:
            return "codex"
        return "openai"

    def _session_backend(self, session: Any) -> str:
        if self._is_browser_use_session(session):
            return "browser_use"
        return self._configured_backend()

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.observe is None:
            return
        try:
            self.observe.emit(event_type, payload=payload)
        except Exception:
            logger.debug("computer diagnostic emit failed: %s", event_type, exc_info=True)


def _instruction_hash(instruction: str) -> str:
    return hashlib.sha256((instruction or "").encode("utf-8")).hexdigest()[:16]


def _instruction_preview(instruction: str) -> str:
    compact = " ".join((instruction or "").split())
    return redact_text(compact, limit=180)


def _domains_for_browser_task(
    task: str | None,
    current_url: str | None = None,
    *,
    sensitive_urls: list[str] | None = None,
) -> list[str]:
    domains: list[str] = []
    for value in [current_url, *_URL_RE.findall(task or "")]:
        host = _host_from_url(value)
        if host and host not in domains:
            domains.append(host)
    lowered = (task or "").lower()
    for value in sensitive_urls or []:
        host = _host_from_url(value)
        if not host:
            continue
        brand = host.rsplit(".", 1)[0]
        if brand and re.search(rf"\b{re.escape(brand)}\b", lowered) and host not in domains:
            domains.append(host)
    return domains


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


def _url_origin(url: str | None) -> str | None:
    if not url:
        return None
    text = str(url).strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        parsed = urlparse(text)
    except Exception:
        return None
    if not parsed.scheme or not parsed.hostname:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}{port}"


def _canonical_hash(value: Any) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=repr)
    except TypeError:
        encoded = repr(value)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _approval_action_hash(action: dict[str, Any] | None) -> str:
    excluded = {"approval_id", "approval_token", "approved", "approval_scope"}
    payload = {k: v for k, v in dict(action or {}).items() if k not in excluded}
    return _canonical_hash(payload)


def _normalized_domains(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list | tuple | set):
        values = list(value)
    else:
        values = []
    domains: list[str] = []
    for item in values:
        host = _host_from_url(str(item))
        if host and host not in domains:
            domains.append(host)
    return domains
