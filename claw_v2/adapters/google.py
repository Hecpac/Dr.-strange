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
    coerce_usage_dict,
)
from claw_v2.types import LLMResponse


class GoogleAdapter(ProviderAdapter):
    provider_name = "google"
    tool_capable = False

    def __init__(
        self,
        transport: Callable[[LLMRequest], LLMResponse] | None = None,
        *,
        api_key: str | None = None,
    ) -> None:
        self._transport = transport
        self._api_key = api_key

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self._transport is not None:
            return self._transport(request)
        genai = self._load_genai()
        types = import_module("google.genai.types")
        client = genai.Client(api_key=self._api_key) if self._api_key else genai.Client()
        config = None
        system_prompt = build_effective_system_prompt(request)
        if system_prompt:
            config = types.GenerateContentConfig(system_instruction=system_prompt)
        try:
            response = client.models.generate_content(
                model=request.model,
                contents=build_effective_input(request),
                config=config,
            )
        except Exception as exc:  # pragma: no cover - live SDK path
            raise AdapterError(f"Google GenAI request failed: {exc}") from exc
        return LLMResponse(
            content=(getattr(response, "text", None) or "").strip(),
            lane=request.lane,
            provider="google",
            model=request.model,
            confidence=0.7 if getattr(response, "text", None) else 0.0,
            cost_estimate=0.0,
            artifacts={
                "response_id": getattr(response, "response_id", None),
                "usage": coerce_usage_dict(getattr(response, "usage_metadata", None)),
            },
        )

    @staticmethod
    def _load_genai():
        try:
            return import_module("google.genai")
        except ModuleNotFoundError as exc:
            raise AdapterUnavailableError(
                "google.genai is not installed. Install the 'google-genai' Python package to enable Google provider support."
            ) from exc
