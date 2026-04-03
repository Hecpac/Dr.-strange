from __future__ import annotations

import base64
import json
import re
from importlib import import_module
from pathlib import Path
from typing import Any, Callable

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
_THINK_RE = re.compile(r"<\|think\|>(.*?)<\|/think\|>", re.DOTALL)


class OllamaAdapter(ProviderAdapter):
    provider_name = "ollama"
    tool_capable = True

    def __init__(
        self,
        transport: Callable[[LLMRequest], LLMResponse] | None = None,
        *,
        host: str | None = None,
        num_ctx: int = 131072,
        think: bool = True,
    ) -> None:
        self._transport = transport
        self._host = host or _DEFAULT_HOST
        self._num_ctx = num_ctx
        self._think = think

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self._transport is not None:
            return self._transport(request)
        ollama = self._load_sdk()
        client = ollama.Client(host=self._host)

        messages: list[dict[str, Any]] = []
        system_prompt = build_effective_system_prompt(request)
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        user_content = build_effective_input(request)
        user_msg = self._build_user_message(user_content)
        messages.append(user_msg)

        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "options": {"num_ctx": self._num_ctx},
            "think": self._think,
        }

        # Function calling: pass tools if provided
        if request.allowed_tools:
            kwargs["tools"] = self._build_tools(request.allowed_tools)

        try:
            response = client.chat(**kwargs)
        except Exception as exc:
            raise AdapterError(f"Ollama request failed: {exc}") from exc

        content = self._extract_content(response)
        thinking = self._extract_thinking(content)
        if thinking:
            content = _THINK_RE.sub("", content).strip()

        tool_calls = self._extract_tool_calls(response)
        tokens_eval, tokens_prompt = self._extract_tokens(response)

        artifacts: dict[str, Any] = {
            "eval_count": tokens_eval,
            "prompt_eval_count": tokens_prompt,
        }
        if thinking:
            artifacts["thinking"] = thinking
        if tool_calls:
            artifacts["tool_calls"] = tool_calls

        return LLMResponse(
            content=content,
            lane=request.lane,
            provider="ollama",
            model=request.model,
            confidence=0.7 if content else 0.0,
            cost_estimate=0.0,
            artifacts=artifacts,
        )

    # --- Message building ---

    def _build_user_message(self, user_content: str | list) -> dict[str, Any]:
        """Build user message with optional image support."""
        if isinstance(user_content, list):
            text_parts: list[str] = []
            images: list[str] = []
            for block in user_content:
                if isinstance(block, dict):
                    btype = block.get("type", "")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype == "image" and "path" in block:
                        img_b64 = self._encode_image(block["path"])
                        if img_b64:
                            images.append(img_b64)
                    elif btype == "image_url":
                        url = block.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            b64 = url.split(",", 1)[-1]
                            images.append(b64)
                elif isinstance(block, str):
                    text_parts.append(block)
            msg: dict[str, Any] = {"role": "user", "content": "\n".join(text_parts) or "Describe this image."}
            if images:
                msg["images"] = images
            return msg
        return {"role": "user", "content": str(user_content)}

    @staticmethod
    def _encode_image(path: str) -> str | None:
        p = Path(path)
        if p.exists():
            return base64.b64encode(p.read_bytes()).decode()
        return None

    # --- Tool / function calling ---

    @staticmethod
    def _build_tools(allowed_tools: list[str]) -> list[dict[str, Any]]:
        """Convert tool names to Ollama tool format.

        If items are already dicts (full tool schemas), pass them through.
        Otherwise create minimal function stubs.
        """
        tools = []
        for tool in allowed_tools:
            if isinstance(tool, dict):
                tools.append(tool)
            else:
                tools.append({
                    "type": "function",
                    "function": {
                        "name": tool,
                        "description": f"Execute {tool}",
                        "parameters": {"type": "object", "properties": {}},
                    },
                })
        return tools

    # --- Response extraction ---

    @staticmethod
    def _extract_content(response: Any) -> str:
        if isinstance(response, dict):
            return response.get("message", {}).get("content", "")
        msg = getattr(response, "message", None)
        return getattr(msg, "content", "") if msg else ""

    @staticmethod
    def _extract_thinking(content: str) -> str | None:
        match = _THINK_RE.search(content)
        return match.group(1).strip() if match else None

    @staticmethod
    def _extract_tool_calls(response: Any) -> list[dict[str, Any]]:
        if isinstance(response, dict):
            msg = response.get("message", {})
            raw_calls = msg.get("tool_calls", [])
        else:
            msg = getattr(response, "message", None)
            raw_calls = getattr(msg, "tool_calls", []) or []
        calls = []
        for tc in raw_calls:
            if isinstance(tc, dict):
                fn = tc.get("function", {})
                calls.append({"name": fn.get("name", ""), "arguments": fn.get("arguments", {})})
            else:
                fn = getattr(tc, "function", None)
                if fn:
                    calls.append({
                        "name": getattr(fn, "name", ""),
                        "arguments": getattr(fn, "arguments", {}),
                    })
        return calls

    @staticmethod
    def _extract_tokens(response: Any) -> tuple[int, int]:
        if isinstance(response, dict):
            return response.get("eval_count", 0), response.get("prompt_eval_count", 0)
        return (
            getattr(response, "eval_count", 0) or 0,
            getattr(response, "prompt_eval_count", 0) or 0,
        )

    @staticmethod
    def _load_sdk():
        try:
            return import_module("ollama")
        except ModuleNotFoundError as exc:
            raise AdapterUnavailableError(
                "ollama is not installed. Install the 'ollama' Python package to enable Ollama provider support."
            ) from exc
