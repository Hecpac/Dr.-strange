from __future__ import annotations

import shutil
import subprocess
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

        cmd = [
            resolved, "exec",
            "--model", model,
            "--full-auto",
            "--color", "never",
        ]
        if request.cwd:
            cmd += ["-C", request.cwd]
        cmd += ["--", full_prompt]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=request.timeout,
                stdin=subprocess.DEVNULL,
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
            confidence=_codex_confidence(content),
            cost_estimate=0.0,
            artifacts={"stderr": result.stderr.strip()[:200]},
        )


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
