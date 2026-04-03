from __future__ import annotations

from importlib import import_module
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

_DEFAULT_HOST = "http://localhost:11434"


class OllamaAdapter(ProviderAdapter):
    provider_name = "ollama"
    tool_capable = False

    def __init__(
        self,
        transport: Callable[[LLMRequest], LLMResponse] | None = None,
        *,
        host: str | None = None,
    ) -> None:
        self._transport = transport
        self._host = host or _DEFAULT_HOST

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self._transport is not None:
            return self._transport(request)
        ollama = self._load_sdk()
        client = ollama.Client(host=self._host)
        messages: list[dict[str, str]] = []
        system_prompt = build_effective_system_prompt(request)
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        user_content = build_effective_input(request)
        if isinstance(user_content, str):
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": str(user_content)})
        try:
            response = client.chat(
                model=request.model,
                messages=messages,
            )
        except Exception as exc:
            raise AdapterError(f"Ollama request failed: {exc}") from exc
        content = ""
        if isinstance(response, dict):
            content = response.get("message", {}).get("content", "")
        else:
            msg = getattr(response, "message", None)
            content = getattr(msg, "content", "") if msg else ""
        tokens_eval = 0
        tokens_prompt = 0
        if isinstance(response, dict):
            tokens_eval = response.get("eval_count", 0)
            tokens_prompt = response.get("prompt_eval_count", 0)
        else:
            tokens_eval = getattr(response, "eval_count", 0) or 0
            tokens_prompt = getattr(response, "prompt_eval_count", 0) or 0
        return LLMResponse(
            content=content.strip(),
            lane=request.lane,
            provider="ollama",
            model=request.model,
            confidence=0.6 if content.strip() else 0.0,
            cost_estimate=0.0,
            artifacts={
                "eval_count": tokens_eval,
                "prompt_eval_count": tokens_prompt,
            },
        )

    @staticmethod
    def _load_sdk():
        try:
            return import_module("ollama")
        except ModuleNotFoundError as exc:
            raise AdapterUnavailableError(
                "ollama is not installed. Install the 'ollama' Python package to enable Ollama provider support."
            ) from exc
