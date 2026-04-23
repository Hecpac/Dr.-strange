from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
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

_SCRUB_ENV_KEYS = frozenset({
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
    "TELEGRAM_BOT_TOKEN", "AWS_SECRET_ACCESS_KEY", "HEYGEN_API_KEY",
    "FIRECRAWL_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN_SECRET",
})


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
    ) -> None:
        self.cli_path = cli_path
        self._transport = transport

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self._transport is not None:
            return self._transport(request)
        return self._run_cli(request)

    def _run_cli(self, request: LLMRequest) -> LLMResponse:
        resolved = shutil.which(self.cli_path)
        if not resolved:
            raise AdapterUnavailableError(
                f"Codex CLI not found at '{self.cli_path}'. "
                "Install with: npm install -g @openai/codex"
            )

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

        workspace = str(Path.home() / "Projects" / "Dr.-strange")
        cwd = request.cwd or workspace
        if not Path(cwd).resolve().is_relative_to(Path(workspace).resolve()):
            raise AdapterError(f"Codex cwd '{cwd}' is outside workspace root.")

        cmd = [
            resolved, "exec",
            "--model", model,
            "--full-auto",
            "--color", "never",
        ]
        if cwd != workspace:
            cmd += ["-C", cwd]
        cmd += ["--", full_prompt]

        env = {k: v for k, v in os.environ.items() if k not in _SCRUB_ENV_KEYS}

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=request.timeout,
                stdin=subprocess.DEVNULL,
                env=env,
            )
        except FileNotFoundError as exc:
            raise AdapterUnavailableError(
                f"Codex CLI not found at '{self.cli_path}'."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AdapterError(
                f"Codex CLI timed out after {request.timeout}s"
            ) from exc

        if result.returncode != 0:
            raise AdapterError(
                f"Codex CLI failed (exit {result.returncode}): {result.stderr.strip()[:500]}"
            )

        content = result.stdout.strip()
        return LLMResponse(
            content=content,
            lane=request.lane,
            provider="codex",
            model=model,
            confidence=0.7 if content else 0.3,
            cost_estimate=0.0,
            artifacts={"stderr": result.stderr.strip()[:200]},
        )
