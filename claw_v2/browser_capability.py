from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path
from typing import Any, Callable

from claw_v2.chrome import ManagedChrome

logger = logging.getLogger(__name__)

DEFAULT_CDP_PORT = 9250
DEFAULT_CDP_PROFILE_DIR = "~/.claw/chrome-profile"
DEFAULT_CDP_HOST = "127.0.0.1"


class BrowserCapabilityError(RuntimeError):
    """Raised when Chrome CDP cannot be prepared for browser automation."""

    def __init__(self, message: str, *, endpoint: str) -> None:
        super().__init__(message)
        self.endpoint = endpoint


class BrowserCapability:
    """Preflight and self-heal the local Chrome CDP runtime."""

    def __init__(
        self,
        *,
        observe: Any | None = None,
        chrome_factory: Callable[..., Any] = ManagedChrome,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        probe_timeout: float = 2.0,
    ) -> None:
        self.observe = observe
        self._chrome_factory = chrome_factory
        self._urlopen = urlopen
        self._probe_timeout = probe_timeout

    def ensure_ready(
        self,
        port: int = DEFAULT_CDP_PORT,
        profile_dir: str = DEFAULT_CDP_PROFILE_DIR,
        visible: bool = True,
    ) -> str:
        """Ensure Chrome CDP responds on /json/version, starting it if needed."""
        port = _normalize_port(port)
        endpoint = f"http://{DEFAULT_CDP_HOST}:{port}"
        profile_path = str(Path(profile_dir).expanduser().resolve(strict=False))
        payload = {
            "endpoint": endpoint,
            "port": port,
            "profile_dir": profile_path,
        }
        self._emit("browser_capability_preflight_started", payload)

        first_error = self._probe_json_version(endpoint, visible=visible)
        if first_error is None:
            if visible:
                try:
                    chrome = self._chrome_factory(
                        port=port,
                        profile_dir=profile_dir,
                        observe=self._managed_chrome_observe,
                    )
                    chrome.ensure(headless=False)
                except Exception as exc:
                    message = (
                        "Necesito abrir/login Chrome para esta tarea de navegador. "
                        f"CDP responde en {endpoint}, pero no pude preparar una "
                        f"ventana visible con perfil {profile_path}: {_error_message(exc)}"
                    )
                    self._fail(payload, message, stage="focus_existing_chrome", first_error=first_error)
                    raise BrowserCapabilityError(message, endpoint=endpoint) from exc
            self._emit(
                "browser_capability_preflight_ok",
                {**payload, "started_chrome": False, "focused_chrome": bool(visible)},
            )
            return endpoint

        try:
            chrome = self._chrome_factory(
                port=port,
                profile_dir=profile_dir,
                observe=self._managed_chrome_observe,
            )
            chrome.ensure(headless=not visible)
        except Exception as exc:
            message = (
                "Necesito abrir/login Chrome para esta tarea de navegador. "
                f"Intenté preparar Chrome/CDP en {endpoint} con perfil {profile_path}, "
                f"pero falló: {_error_message(exc)}"
            )
            self._fail(payload, message, stage="start_chrome", first_error=first_error)
            raise BrowserCapabilityError(message, endpoint=endpoint) from exc

        second_error = self._probe_json_version(endpoint, visible=visible)
        if second_error is not None:
            message = (
                "Necesito abrir/login Chrome para esta tarea de navegador. "
                f"Chrome se inició o se reutilizó, pero {endpoint}/json/version "
                f"no respondió: {second_error}"
            )
            self._fail(payload, message, stage="verify_after_start", first_error=first_error)
            raise BrowserCapabilityError(message, endpoint=endpoint)

        self._emit(
            "browser_capability_preflight_ok",
            {**payload, "started_chrome": True},
        )
        return endpoint

    def _probe_json_version(self, endpoint: str, *, visible: bool = True) -> str | None:
        url = f"{endpoint}/json/version"
        try:
            response = self._urlopen(url, timeout=self._probe_timeout)
            if hasattr(response, "__enter__"):
                with response as opened:
                    self._read_version_response(opened, visible=visible)
            else:
                try:
                    self._read_version_response(response, visible=visible)
                finally:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
            return None
        except Exception as exc:
            return _error_message(exc)

    @staticmethod
    def _read_version_response(response: Any, *, visible: bool = True) -> None:
        status = getattr(response, "status", getattr(response, "code", None))
        if status is not None and int(status) >= 400:
            raise RuntimeError(f"HTTP {status}")
        raw = response.read(8192)
        if raw:
            data = json.loads(raw.decode("utf-8"))
            if visible:
                browser = str(data.get("Browser") or "")
                user_agent = str(data.get("User-Agent") or "")
                if "HeadlessChrome" in browser or "HeadlessChrome" in user_agent:
                    raise RuntimeError("Chrome CDP is headless; visible Chrome required")

    def _fail(
        self,
        payload: dict[str, Any],
        message: str,
        *,
        stage: str,
        first_error: str | None,
    ) -> None:
        self._emit(
            "browser_capability_preflight_failed",
            {
                **payload,
                "stage": stage,
                "first_probe_error": (first_error or "")[:200],
                "error": message[:300],
            },
        )

    def _managed_chrome_observe(self, event_type: str, payload: dict) -> None:
        self._emit(event_type, payload)

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.observe is None:
            return
        try:
            emit = getattr(self.observe, "emit", None)
            if callable(emit):
                emit(event_type, payload=payload)
            elif callable(self.observe):
                self.observe(event_type, payload)
        except Exception:
            logger.debug("browser capability observe emit failed: %s", event_type, exc_info=True)


def _normalize_port(port: int) -> int:
    try:
        normalized = int(port)
    except (TypeError, ValueError) as exc:
        raise BrowserCapabilityError(
            f"Necesito abrir/login Chrome para esta tarea de navegador: puerto CDP invalido ({port!r}).",
            endpoint=f"http://{DEFAULT_CDP_HOST}:{DEFAULT_CDP_PORT}",
        ) from exc
    if normalized <= 0 or normalized > 65535:
        raise BrowserCapabilityError(
            f"Necesito abrir/login Chrome para esta tarea de navegador: puerto CDP invalido ({port!r}).",
            endpoint=f"http://{DEFAULT_CDP_HOST}:{DEFAULT_CDP_PORT}",
        )
    return normalized


def _error_message(exc: BaseException) -> str:
    message = str(exc).strip()
    return message or exc.__class__.__name__
