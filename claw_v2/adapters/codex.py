from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import time
from typing import Callable

from claw_v2.adapters.base import (
    AdapterError,
    AdapterUnavailableError,
    LLMRequest,
    ProviderAdapter,
    build_effective_input,
    build_effective_system_prompt,
)
from claw_v2.types import LLMResponse


@dataclass(slots=True)
class _CodexPreflight:
    resolved_path: str
    version: str
    auth_status: str
    checked_at: float


class CodexAdapter(ProviderAdapter):
    """Provider adapter that invokes the Codex CLI via subprocess.

    Uses the ChatGPT Pro subscription — cost_estimate is always 0.0.
    tool_capable=True because Codex CLI handles its own internal tool loop
    (file edits, shell execution, etc.).
    """

    provider_name = "codex"
    tool_capable = True

    def __init__(
        self,
        cli_path: str = "codex",
        *,
        transport: Callable[[LLMRequest], LLMResponse] | None = None,
        preflight_enabled: bool = True,
        preflight_timeout: float = 5.0,
        preflight_ttl_seconds: float = 300.0,
    ) -> None:
        self.cli_path = cli_path
        self._transport = transport
        self._preflight_enabled = preflight_enabled
        self._preflight_timeout = preflight_timeout
        self._preflight_ttl_seconds = preflight_ttl_seconds
        self._preflight_cache: _CodexPreflight | None = None

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self._transport is not None:
            return self._transport(request)
        return self._run_cli(request)

    def _run_cli(self, request: LLMRequest) -> LLMResponse:
        preflight = self._preflight(request)
        resolved = preflight.resolved_path

        prompt = build_effective_input(request)
        if isinstance(prompt, list):
            prompt = " ".join(
                block.get("text", "")
                for block in prompt
                if isinstance(block, dict) and block.get("type") == "text"
            )

        model = request.model or "codex-mini-latest"
        system = build_effective_system_prompt(request)

        full_prompt = f"{system}\n\n{prompt}" if system else str(prompt)

        cmd = [
            resolved, "exec",
            "--model", model,
            "--full-auto",
            "--color", "never",
        ]
        if request.cwd:
            cmd += ["-C", request.cwd]

        result = None
        for attempt in range(2):
            try:
                result = subprocess.run(
                    cmd,
                    input=full_prompt,
                    capture_output=True,
                    text=True,
                    timeout=request.timeout,
                )
            except FileNotFoundError as exc:
                raise AdapterUnavailableError(
                    f"Codex CLI not found at '{self.cli_path}'."
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise AdapterError(
                    f"Codex CLI timed out after {request.timeout}s"
                ) from exc

            if result.returncode == 0:
                break
            if attempt == 0 and _is_retryable_startup_failure(result):
                continue
            if _is_auth_failure_result(result):
                raise AdapterUnavailableError(
                    "Codex CLI is not authenticated or the session expired. "
                    f"Run `codex login` and retry. Detail: {_format_cli_detail(result)}"
                )
            raise AdapterError(_format_cli_failure(result))

        if result is None:  # pragma: no cover - defensive, loop always assigns
            raise AdapterError("Codex CLI failed before producing a result")

        content = result.stdout.strip()
        return LLMResponse(
            content=content,
            lane=request.lane,
            provider="codex",
            model=model,
            confidence=_codex_confidence(content),
            cost_estimate=0.0,
            artifacts={
                "stderr": result.stderr.strip()[:200],
                "codex_version": preflight.version,
                "auth_status": preflight.auth_status[:200],
            },
        )

    def _preflight(self, request: LLMRequest) -> _CodexPreflight:
        resolved = shutil.which(self.cli_path)
        if not resolved:
            raise AdapterUnavailableError(
                f"Codex CLI not found at '{self.cli_path}'. "
                "Install with: npm install -g @openai/codex"
            )
        if request.cwd:
            cwd = Path(request.cwd).expanduser()
            if not cwd.exists():
                raise AdapterUnavailableError(f"Codex CLI cwd does not exist: {request.cwd}")
            if not cwd.is_dir():
                raise AdapterUnavailableError(f"Codex CLI cwd is not a directory: {request.cwd}")
        if not self._preflight_enabled:
            return _CodexPreflight(
                resolved_path=resolved,
                version="preflight disabled",
                auth_status="preflight disabled",
                checked_at=time.monotonic(),
            )
        now = time.monotonic()
        cached = self._preflight_cache
        if (
            cached is not None
            and cached.resolved_path == resolved
            and self._preflight_ttl_seconds > 0
            and now - cached.checked_at < self._preflight_ttl_seconds
        ):
            return cached

        version = self._run_preflight_command([resolved, "--version"], label="version")
        auth_status = self._run_preflight_command([resolved, "login", "status"], label="auth")
        preflight = _CodexPreflight(
            resolved_path=resolved,
            version=version,
            auth_status=auth_status,
            checked_at=now,
        )
        self._preflight_cache = preflight
        return preflight

    def _run_preflight_command(self, cmd: list[str], *, label: str) -> str:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._preflight_timeout,
            )
        except FileNotFoundError as exc:
            raise AdapterUnavailableError(f"Codex CLI not found at '{self.cli_path}'.") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdapterUnavailableError(
                f"Codex CLI {label} preflight timed out after {self._preflight_timeout}s."
            ) from exc

        detail = _format_cli_detail(result)
        if result.returncode != 0:
            if label == "auth" or _is_auth_failure_result(result):
                raise AdapterUnavailableError(
                    "Codex CLI authentication preflight failed. "
                    f"Run `codex login` and retry. Detail: {detail}"
                )
            raise AdapterUnavailableError(
                f"Codex CLI {label} preflight failed (exit {result.returncode}): {detail}"
            )
        if label == "auth" and _is_auth_failure_text(detail):
            raise AdapterUnavailableError(
                "Codex CLI authentication preflight failed. "
                f"Run `codex login` and retry. Detail: {detail}"
            )
        return detail if detail != "(no output)" else "ok"


def _codex_confidence(content: str) -> float:
    if not content:
        return 0.3
    structured = sum(
        1 for marker in ("```", "\n## ", "\n# ", "\n- ", "\n* ", "/", "diff --git", "## Edits", "## Build/Verify", "## Evidence")
        if marker in content
    )
    if structured >= 3 and len(content) >= 200:
        return 0.85
    if structured >= 1:
        return 0.7
    return 0.55


def _is_retryable_startup_failure(result: subprocess.CompletedProcess[str]) -> bool:
    if result.stdout.strip():
        return False
    stderr = (result.stderr or "").lower()
    return "reading additional input from stdin" in stderr


def _is_auth_failure_result(result: subprocess.CompletedProcess[str]) -> bool:
    return _is_auth_failure_text(f"{result.stdout or ''}\n{result.stderr or ''}")


def _is_auth_failure_text(text: str) -> bool:
    normalized = text.lower()
    return any(
        marker in normalized
        for marker in (
            "not logged in",
            "not authenticated",
            "authentication required",
            "login required",
            "please log in",
            "unauthorized",
            "invalid api key",
            "expired session",
            "401",
            "403",
        )
    )


def _format_cli_detail(result: subprocess.CompletedProcess[str]) -> str:
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    detail = stderr or stdout or "(no output)"
    if stderr and stdout:
        detail = f"{stderr[:350]} | stdout: {stdout[:150]}"
    return detail[:500]


def _format_cli_failure(result: subprocess.CompletedProcess[str]) -> str:
    return f"Codex CLI failed (exit {result.returncode}): {_format_cli_detail(result)}"
