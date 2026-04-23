from __future__ import annotations

import json
import logging
from importlib import import_module
from typing import Any, Callable

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

logger = logging.getLogger(__name__)

_MAX_TOOL_ROUNDS = 15


class OpenAIAdapter(ProviderAdapter):
    provider_name = "openai"

    def __init__(
        self,
        transport: Callable[[LLMRequest], LLMResponse] | None = None,
        *,
        api_key: str | None = None,
        tool_executor: Callable[[str, dict], dict] | None = None,
        tool_schemas: list[dict] | None = None,
    ) -> None:
        self._transport = transport
        self._api_key = api_key
        self._tool_executor = tool_executor
        self._tool_schemas = tool_schemas or []

    @property
    def tool_capable(self) -> bool:  # type: ignore[override]
        return bool(self._tool_executor and self._tool_schemas)

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self._transport is not None:
            return self._transport(request)
        sdk = self._load_sdk()
        client = sdk.OpenAI(api_key=self._api_key) if self._api_key else sdk.OpenAI()
        try:
            kwargs: dict[str, Any] = dict(
                model=request.model,
                input=_normalize_input(build_effective_input(request)),
                instructions=build_effective_system_prompt(request),
                previous_response_id=request.session_id,
            )
            if request.effort and _supports_reasoning(request.model):
                kwargs["reasoning"] = {"effort": request.effort}

            # Attach tools only when the caller explicitly listed allowed names
            use_tools = self.tool_capable and request.allowed_tools
            if use_tools:
                allowed = set(request.allowed_tools)
                schemas = [s for s in self._tool_schemas if s["name"] in allowed]
                if schemas:
                    kwargs["tools"] = schemas

            response = client.responses.create(**kwargs)

            # Tool-calling loop
            if use_tools:
                response = self._tool_loop(client, request, response)

        except Exception as exc:
            raise AdapterError(f"OpenAI Responses request failed: {exc}") from exc

        return self._build_response(response, request)

    def _tool_loop(self, client: Any, request: LLMRequest, response: Any) -> Any:
        """Execute function calls in a loop until the model stops calling tools."""
        for _ in range(_MAX_TOOL_ROUNDS):
            function_calls = [
                item for item in getattr(response, "output", [])
                if getattr(item, "type", None) == "function_call"
            ]
            if not function_calls:
                break

            tool_results: list[dict[str, Any]] = []
            for call in function_calls:
                name = getattr(call, "name", "")
                call_id = getattr(call, "call_id", "")
                try:
                    arguments = json.loads(getattr(call, "arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    arguments = {}

                try:
                    result = self._tool_executor(name, arguments)  # type: ignore[misc]
                    output = json.dumps(result, default=str)
                except Exception as exc:
                    output = json.dumps({"error": str(exc)})
                    logger.warning("OpenAI tool %s failed: %s", name, exc)

                tool_results.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output[:50_000],  # safety cap
                })

            kwargs: dict[str, Any] = dict(
                model=request.model,
                previous_response_id=getattr(response, "id", None),
                input=tool_results,
            )
            if request.effort and _supports_reasoning(request.model):
                kwargs["reasoning"] = {"effort": request.effort}
            response = client.responses.create(**kwargs)

        return response

    def _build_response(self, response: Any, request: LLMRequest) -> LLMResponse:
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


def _normalize_input(prompt: str | list) -> str | list:
    """Convert Anthropic-style content blocks to OpenAI Responses API format."""
    if isinstance(prompt, str):
        return prompt
    if not isinstance(prompt, list):
        return str(prompt)
    if not prompt:
        return prompt
    first = prompt[0]
    if isinstance(first, dict) and first.get("type") in ("text", "image"):
        content: list[dict[str, Any]] = []
        for block in prompt:
            btype = block.get("type", "")
            if btype == "text":
                content.append({"type": "input_text", "text": block.get("text", "")})
            elif btype == "image":
                src = block.get("source", {})
                url = src.get("url") or f"data:{src.get('media_type','image/png')};base64,{src.get('data','')}"
                content.append({"type": "input_image", "image_url": url})
            else:
                content.append({"type": "input_text", "text": json.dumps(block, default=str)})
        return [{"type": "message", "role": "user", "content": content}]
    return prompt


_REASONING_MODELS = {"o3", "o3-mini", "o4-mini", "gpt-5.4", "gpt-5.4-mini"}


def _supports_reasoning(model: str) -> bool:
    return any(model.startswith(prefix) for prefix in _REASONING_MODELS)
