from __future__ import annotations

import json
import logging
import time
from importlib import import_module
from typing import Any, Callable

from claw_v2.adapters.base import (
    ADVISORY_LANES,
    AdapterError,
    AdapterUnavailableError,
    LLMRequest,
    ProviderAdapter,
    StreamInterruptedError,
    build_effective_input,
    build_effective_system_prompt,
    coerce_usage_dict,
    record_tools_executed,
)
from claw_v2.approval_gate import ApprovalPending
from claw_v2.pricing import estimate_cost_usd
from claw_v2.types import LLMResponse

logger = logging.getLogger(__name__)

_MAX_TOOL_ROUNDS = 15
_OPENAI_RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}
_OPENAI_MAX_ATTEMPTS = 3


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
            try:
                return self._transport(request)
            except AdapterError as exc:
                if not _is_stale_previous_response_error(exc):
                    raise
                if not request.session_id:
                    raise
                stale = request.session_id
                fresh_request = _request_without_session(request)
                response = self._transport(fresh_request)
                response.artifacts["session_recovery"] = "previous_response_id_reset"
                response.artifacts["stale_session_id"] = stale
                return response
        try:
            return self._complete_via_sdk(request)
        except AdapterError as exc:
            if not _is_stale_previous_response_error(exc):
                raise
            if not request.session_id:
                raise
            stale = request.session_id
            fresh_request = _request_without_session(request)
            response = self._complete_via_sdk(fresh_request)
            response.artifacts["session_recovery"] = "previous_response_id_reset"
            response.artifacts["stale_session_id"] = stale
            return response

    def _complete_via_sdk(self, request: LLMRequest) -> LLMResponse:
        sdk = self._load_sdk()
        client = sdk.OpenAI(api_key=self._api_key) if self._api_key else sdk.OpenAI()
        # Enforce the validated per-request timeout on every HTTP call
        # (initial response and each tool-loop round); it was previously
        # never passed to the client, so a hung call blocked indefinitely.
        client = client.with_options(timeout=request.timeout)
        # Registry tools have no read-only marker here, so any executed tool
        # counts: a later failure must not be replayed by fallback/retry.
        executed_tools: list[str] = []
        # Usage is reported per HTTP response; collect every round so
        # multi-round tool turns meter real spend, not just the last call.
        usage_rounds: list[dict[str, Any]] = []
        try:
            kwargs: dict[str, Any] = dict(
                model=request.model,
                input=_normalize_responses_input(build_effective_input(request)),
                instructions=build_effective_system_prompt(request),
            )
            previous_response_id = _valid_previous_response_id(request.session_id)
            if previous_response_id:
                kwargs["previous_response_id"] = previous_response_id
            if request.effort and _supports_reasoning(request.model):
                kwargs["reasoning"] = {"effort": request.effort}

            # Non-advisory lanes are expected to stay operational after a
            # cross-provider fallback. A None allowlist means "use the default
            # tool surface" for these lanes, matching the Anthropic adapter.
            use_tools = self.tool_capable and (
                request.allowed_tools is not None or request.lane not in ADVISORY_LANES
            )
            if use_tools:
                allowed = set(request.allowed_tools or [])
                schemas = [s for s in self._tool_schemas if not allowed or s["name"] in allowed]
                if schemas:
                    kwargs["tools"] = schemas

            response = _create_response_with_retry(client, kwargs)
            _record_round_usage(usage_rounds, response)
            # Enforce the cap on the very first response too: advisory lanes
            # and no-tool turns never enter the loop, so without this check an
            # oversized billable response would be returned as successful.
            _enforce_request_budget(request, usage_rounds)

            # Tool-calling loop
            if use_tools:
                response = self._tool_loop(client, request, response, executed_tools, usage_rounds)

        except ApprovalPending:
            # Tier 3 soft-block — propagate past the adapter boundary so the
            # bot can format /approve for Hector.
            raise
        except AdapterError as exc:
            record_tools_executed(exc, executed_tools)
            raise
        except Exception as exc:
            if _is_stream_idle_error(exc):
                error: AdapterError = StreamInterruptedError(
                    f"OpenAI stream interrupted: {exc}",
                    partial_output=_extract_partial_output(exc),
                )
            else:
                error = AdapterError(f"OpenAI Responses request failed: {exc}")
            record_tools_executed(error, executed_tools)
            raise error from exc

        return self._build_response(response, request, usage_rounds)

    def _tool_loop(
        self,
        client: Any,
        request: LLMRequest,
        response: Any,
        executed_tools: list[str] | None = None,
        usage_rounds: list[dict[str, Any]] | None = None,
    ) -> Any:
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

                if executed_tools is not None and name:
                    executed_tools.append(name)
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
            response = _create_response_with_retry(client, kwargs)
            if usage_rounds is not None:
                _record_round_usage(usage_rounds, response)
                # OpenAI is API-billed (subscriptions cover Claude/Codex, not
                # this adapter): enforce the per-request cap the Claude SDK
                # already honors, instead of looping up to 15 unmetered rounds.
                _enforce_request_budget(request, usage_rounds)

        return response

    def _build_response(
        self,
        response: Any,
        request: LLMRequest,
        usage_rounds: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        rounds = usage_rounds or [coerce_usage_dict(getattr(response, "usage", None))]
        usage = _aggregate_usage(rounds)
        first_estimate = estimate_cost_usd("openai", request.model, rounds[0])
        total_cost, cost_unknown = _estimate_rounds_cost(request.model, rounds)
        return LLMResponse(
            content=(getattr(response, "output_text", None) or "").strip(),
            lane=request.lane,
            provider="openai",
            model=request.model,
            confidence=0.7 if getattr(response, "output_text", None) else 0.0,
            cost_estimate=total_cost,
            cost_unknown=cost_unknown,
            cost_source=first_estimate.price_source,
            cost_price_as_of=first_estimate.price_as_of,
            artifacts={
                "response_id": getattr(response, "id", None),
                "session_id": getattr(response, "id", None),
                "usage": usage,
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


def _record_round_usage(usage_rounds: list[dict[str, Any]], response: Any) -> None:
    usage = coerce_usage_dict(getattr(response, "usage", None))
    if usage:
        usage_rounds.append(usage)


def _aggregate_usage(usage_rounds: list[dict[str, Any]]) -> dict[str, Any]:
    if not usage_rounds:
        return {}
    if len(usage_rounds) == 1:
        return usage_rounds[0]
    total: dict[str, Any] = {}
    for usage in usage_rounds:
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total[key] = total.get(key, 0) + value
    total["rounds"] = len(usage_rounds)
    return total


def _enforce_request_budget(request: LLMRequest, usage_rounds: list[dict[str, Any]]) -> None:
    if request.max_budget <= 0:
        return
    spent, spend_unknown = _estimate_rounds_cost(request.model, usage_rounds)
    if not spend_unknown and spent > request.max_budget:
        raise AdapterError(
            f"OpenAI request exceeded max_budget: ${spent:.4f} > ${request.max_budget:.4f}",
            metadata={"reason": "budget_exceeded", "cost_usd": round(spent, 6)},
        )


def _estimate_rounds_cost(model: str, usage_rounds: list[dict[str, Any]]) -> tuple[float, bool]:
    """Sum the per-round cost estimates. unknown=True when any metered round
    could not be priced (the router then emits cost_metering_unknown)."""
    total = 0.0
    unknown = not usage_rounds
    for usage in usage_rounds:
        estimate = estimate_cost_usd("openai", model, usage)
        if estimate.unknown:
            unknown = True
            continue
        total += estimate.amount_usd
    return total, unknown


_REASONING_MODELS = {"o3", "o3-mini", "o4-mini", "gpt-5.5", "gpt-5.4", "gpt-5.4-mini"}


def _supports_reasoning(model: str) -> bool:
    return any(model.startswith(prefix) for prefix in _REASONING_MODELS)


_STREAM_IDLE_MARKERS = (
    "stream idle timeout",
    "partial response received",
    "stream_idle_timeout",
    "incomplete chunked read",
    "stream interrupted",
)


def _is_stream_idle_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _STREAM_IDLE_MARKERS)


def _extract_partial_output(exc: Exception) -> str:
    partial = getattr(exc, "partial_output", None)
    if isinstance(partial, str):
        return partial
    return ""


_STALE_PREVIOUS_RESPONSE_MARKERS = (
    "previous_response_not_found",
    "invalid 'previous_response_id'",
    "invalid previous_response_id",
    "previous response with id",
    "previous_response_id cannot be resolved",
)


def _is_stale_previous_response_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _STALE_PREVIOUS_RESPONSE_MARKERS)


def _request_without_session(request: LLMRequest) -> LLMRequest:
    from dataclasses import replace
    evidence = dict(request.evidence_pack or {})
    evidence["session_recovery"] = "openai_previous_response_id_reset"
    evidence["stale_session_id"] = request.session_id
    return replace(request, session_id=None, evidence_pack=evidence)


def _valid_previous_response_id(session_id: str | None) -> str | None:
    if not session_id:
        return None
    if session_id.startswith("resp_") or session_id.startswith("resp-"):
        return session_id
    logger.warning("Ignoring non-OpenAI previous_response_id: %s", session_id[:32])
    return None


def _create_response_with_retry(
    client: Any,
    kwargs: dict[str, Any],
    *,
    max_attempts: int = _OPENAI_MAX_ATTEMPTS,
) -> Any:
    for attempt in range(max_attempts):
        try:
            return client.responses.create(**kwargs)
        except ApprovalPending:
            raise
        except Exception as exc:
            if not _is_retryable_openai_error(exc):
                raise
            if attempt >= max_attempts - 1:
                kind = _classify_openai_error(exc)
                raise AdapterError(
                    f"OpenAI Responses request {kind} after {max_attempts} attempts: {exc}"
                ) from exc
            delay = _retry_delay_seconds(exc, attempt)
            if delay > 0:
                time.sleep(delay)
    raise AdapterError("OpenAI Responses request failed after retry loop exhausted.")


def _is_retryable_openai_error(exc: Exception) -> bool:
    status = _status_code(exc)
    if status in _OPENAI_RETRYABLE_STATUS_CODES:
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "rate limit",
            "too many requests",
            "temporarily unavailable",
            "timeout",
            "timed out",
            "connection reset",
            "connection aborted",
        )
    )


def _classify_openai_error(exc: Exception) -> str:
    status = _status_code(exc)
    message = str(exc).lower()
    if status == 429 or "rate limit" in message or "too many requests" in message:
        return "rate_limited"
    return "transient_failure"


def _status_code(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _retry_delay_seconds(exc: Exception, attempt: int) -> float:
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        return min(max(retry_after, 0.0), 2.0)
    return min(0.25 * (2 ** attempt), 2.0)


def _retry_after_seconds(exc: Exception) -> float | None:
    headers = getattr(exc, "headers", None)
    if headers is None:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = None
    if hasattr(headers, "get"):
        value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
        return [_normalize_message_item(item) for item in prompt]
    return [{"type": "message", "role": "user", "content": _normalize_content_blocks(prompt)}]


def _normalize_message_item(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    content = normalized.get("content")
    if isinstance(content, list):
        normalized["content"] = _normalize_content_blocks(content)
    elif isinstance(content, str):
        normalized["content"] = [{"type": "input_text", "text": content}]
    return normalized


def _normalize_content_blocks(blocks: list[Any]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for block in blocks:
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
    return content
