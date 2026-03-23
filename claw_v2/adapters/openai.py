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


class OpenAIAdapter(ProviderAdapter):
    provider_name = "openai"
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
        sdk = self._load_sdk()
        client = sdk.OpenAI(api_key=self._api_key) if self._api_key else sdk.OpenAI()
        try:
            response = client.responses.create(
                model=request.model,
                input=build_effective_input(request),
                instructions=build_effective_system_prompt(request),
                previous_response_id=request.session_id,
            )
        except Exception as exc:  # pragma: no cover - live SDK path
            raise AdapterError(f"OpenAI Responses request failed: {exc}") from exc
        return LLMResponse(
            content=(getattr(response, "output_text", None) or "").strip(),
            lane=request.lane,
            provider="openai",
            model=request.model,
            confidence=0.7 if getattr(response, "output_text", None) else 0.0,
            cost_estimate=0.0,
            artifacts={
                "response_id": getattr(response, "id", None),
                "usage": coerce_usage_dict(getattr(response, "usage", None)),
            },
        )

    @staticmethod
    def _load_sdk():
        try:
            return import_module("openai")
        except ModuleNotFoundError as exc:
            raise AdapterUnavailableError(
                "openai is not installed. Install the 'openai' Python package to enable OpenAI provider support."
            ) from exc
