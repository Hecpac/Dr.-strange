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
from claw_v2.approval_gate import ApprovalPending
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
                input=_normalize_responses_input(build_effective_input(request)),
                instructions=build_effective_system_prompt(request),
                previous_response_id=request.session_id,
            )
            if request.effort and _supports_reasoning(request.model):
                kwargs["reasoning"] = {"effort": request.effort}

            # Attach tools if available and requested
            use_tools = self.tool_capable and request.allowed_tools is not None
            if use_tools:
                allowed = set(request.allowed_tools or [])
                schemas = [s for s in self._tool_schemas if not allowed or s["name"] in allowed]
                if schemas:
                    kwargs["tools"] = schemas

            response = client.responses.create(**kwargs)

            # Tool-calling loop
            if use_tools:
                response = self._tool_loop(client, request, response)

        except ApprovalPending:
            # Tier 3 soft-block — propagate past the adapter boundary so the
            # bot can format /approve for Hector.
            raise
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
                except ApprovalPending:
                    # Tier 3 pending approval is a soft block, not a tool error:
                    # propagate so the bot can surface /approve to Hector.
                    raise
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


_REASONING_MODELS = {"o3", "o3-mini", "o4-mini", "gpt-5.4", "gpt-5.4-mini"}


def _supports_reasoning(model: str) -> bool:
    return any(model.startswith(prefix) for prefix in _REASONING_MODELS)


def _normalize_responses_input(prompt: Any) -> Any:
    """Wrap content-block lists in a Responses-API message envelope.

    The Responses API rejects top-level items with type='text' or type='image';
    multimodal content must live inside a {"type": "message", "role": "user", "content": [...]}
    envelope, with blocks translated to input_text / input_image.
    """
    if isinstance(prompt, str):
        return prompt
    if not isinstance(prompt, list):
        return prompt
    if all(isinstance(item, dict) and item.get("type") == "message" for item in prompt):
        return prompt
    content: list[dict[str, Any]] = []
    for block in prompt:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype in {"text", "input_text"}:
            content.append({"type": "input_text", "text": block.get("text", "")})
        elif btype == "image":
            source = block.get("source") or {}
            if source.get("type") == "base64":
                url = f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"
            else:
                url = source.get("url", "")
            content.append({"type": "input_image", "image_url": url})
        elif btype == "input_image":
            content.append(block)
        elif btype == "image_url":
            url = block.get("image_url")
            if isinstance(url, dict):
                url = url.get("url", "")
            content.append({"type": "input_image", "image_url": url or ""})
        else:
            content.append(block)
    return [{"type": "message", "role": "user", "content": content}]
