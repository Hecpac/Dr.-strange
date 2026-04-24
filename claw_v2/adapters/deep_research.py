from __future__ import annotations

import logging
import time
from importlib import import_module
from typing import Callable

from claw_v2.adapters.base import (
    AdapterError,
    AdapterUnavailableError,
    LLMRequest,
    ProviderAdapter,
    build_effective_input,
)
from claw_v2.types import LLMResponse

logger = logging.getLogger(__name__)

_DEFAULT_AGENT = "deep-research-preview-04-2026"
_MAX_AGENT = "deep-research-max-preview-04-2026"
_POLL_INTERVAL = 15
_MAX_POLLS = 80  # 20 minutes max


class DeepResearchAdapter(ProviderAdapter):
    """Adapter for Gemini Deep Research via the Interactions API.

    Runs background research tasks that autonomously plan, search,
    and synthesize multi-step queries into cited reports.
    """

    provider_name = "deep_research"
    tool_capable = False

    def __init__(
        self,
        transport: Callable[[LLMRequest], LLMResponse] | None = None,
        *,
        api_key: str | None = None,
        use_max: bool = False,
    ) -> None:
        self._transport = transport
        self._api_key = api_key
        self._agent = _MAX_AGENT if use_max else _DEFAULT_AGENT

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self._transport is not None:
            return self._transport(request)

        genai = self._load_genai()
        client = genai.Client(api_key=self._api_key) if self._api_key else genai.Client()
        query = build_effective_input(request)
        if isinstance(query, list):
            query = " ".join(
                block.get("text", "")
                for block in query
                if isinstance(block, dict) and block.get("type") == "text"
            )

        try:
            interaction = client.interactions.create(
                agent=self._agent,
                input=str(query),
                background=True,
            )
        except Exception as exc:
            raise AdapterError(f"Deep Research create failed: {exc}") from exc

        interaction_id = interaction.id
        logger.info("Deep Research started: %s (agent=%s)", interaction_id, self._agent)

        content = ""
        status = "running"
        for poll in range(_MAX_POLLS):
            time.sleep(_POLL_INTERVAL)
            try:
                result = client.interactions.get(interaction_id)
                status = getattr(result, "status", "unknown")
            except Exception as exc:
                logger.warning("Deep Research poll %d failed: %s", poll, exc)
                continue

            if status == "completed":
                content = (
                    getattr(result, "output_text", "")
                    or getattr(result, "text", "")
                    or getattr(result, "output", "")
                    or ""
                )
                break
            if status == "failed":
                error_msg = getattr(result, "error", "unknown error")
                raise AdapterError(f"Deep Research failed: {error_msg}")

        if status != "completed":
            raise AdapterError(
                f"Deep Research timed out after {_MAX_POLLS * _POLL_INTERVAL}s "
                f"(last status: {status})"
            )

        return LLMResponse(
            content=content.strip(),
            lane=request.lane,
            provider="deep_research",
            model=self._agent,
            confidence=0.8 if content else 0.0,
            cost_estimate=0.0,
            artifacts={"interaction_id": interaction_id},
        )

    @staticmethod
    def _load_genai():
        try:
            return import_module("google.genai")
        except ModuleNotFoundError as exc:
            raise AdapterUnavailableError(
                "google.genai is not installed. Install the 'google-genai' Python package."
            ) from exc
