from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

from claw_v2.types import Lane, LLMResponse


class AdapterError(RuntimeError):
    """Base error for provider adapter failures."""

    def __init__(self, message: str, *, metadata: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.metadata: dict[str, Any] = dict(metadata or {})


class AdapterUnavailableError(AdapterError):
    """Raised when a provider adapter cannot be used in the current environment."""


class StreamInterruptedError(AdapterError):
    """Raised when the provider stream was cut off mid-response (idle/timeout)."""

    def __init__(
        self,
        message: str = "stream_interrupted",
        *,
        partial_output: str = "",
        retryable: bool = True,
        reason: str = "stream_idle_timeout",
    ) -> None:
        super().__init__(
            message,
            metadata={
                "reason": reason,
                "partial_output": partial_output[:4000],
                "retryable": retryable,
            },
        )
        self.partial_output = partial_output
        self.retryable = retryable
        self.reason = reason


UserContentBlock = dict[str, Any]
UserPrompt = str | list[UserContentBlock]
PreLLMHook = Callable[["LLMRequest"], "LLMRequest | None"]
PostLLMHook = Callable[["LLMRequest", LLMResponse], LLMResponse]


@dataclass(slots=True)
class LLMRequest:
    prompt: UserPrompt
    system_prompt: str | None
    lane: Lane
    provider: str
    model: str
    effort: str | None
    session_id: str | None
    max_budget: float
    evidence_pack: dict[str, Any] | None
    allowed_tools: list[str] | None
    agents: dict[str, Any] | None
    hooks: dict[str, Any] | None
    timeout: float
    cwd: str | None = None
    cache_ttl: int | None = None

    def validate(self) -> None:
        """Fail fast on malformed cross-provider requests before adapter calls."""
        if self.lane not in {"brain", "worker", "verifier", "research", "judge"}:
            raise ValueError(f"Invalid lane: {self.lane!r}")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("LLM provider must be a non-empty string.")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("LLM model must be a non-empty string.")
        if isinstance(self.prompt, str):
            if not self.prompt.strip():
                raise ValueError("LLM prompt must be non-empty.")
        elif isinstance(self.prompt, list):
            if not self.prompt:
                raise ValueError("LLM prompt content blocks must be non-empty.")
            for index, block in enumerate(self.prompt):
                if not isinstance(block, dict):
                    raise ValueError(f"LLM prompt content block {index} must be a dict.")
        else:
            raise ValueError("LLM prompt must be a string or list of content blocks.")
        if self.system_prompt is not None and not isinstance(self.system_prompt, str):
            raise ValueError("LLM system_prompt must be a string when provided.")
        if self.effort is not None and (not isinstance(self.effort, str) or not self.effort.strip()):
            raise ValueError("LLM effort must be a non-empty string when provided.")
        if self.session_id is not None and not isinstance(self.session_id, str):
            raise ValueError("LLM session_id must be a string when provided.")
        if not _finite_number(self.max_budget) or self.max_budget < 0:
            raise ValueError("LLM max_budget must be a non-negative finite number.")
        if not _finite_number(self.timeout) or self.timeout <= 0:
            raise ValueError("LLM timeout must be a positive finite number.")
        if self.evidence_pack is not None and not isinstance(self.evidence_pack, dict):
            raise ValueError("LLM evidence_pack must be a dict when provided.")
        if self.allowed_tools is not None:
            if not isinstance(self.allowed_tools, list):
                raise ValueError("LLM allowed_tools must be a list when provided.")
            if any(not isinstance(tool, str) or not tool.strip() for tool in self.allowed_tools):
                raise ValueError("LLM allowed_tools entries must be non-empty strings.")
        if self.agents is not None and not isinstance(self.agents, dict):
            raise ValueError("LLM agents must be a dict when provided.")
        if self.hooks is not None and not isinstance(self.hooks, dict):
            raise ValueError("LLM hooks must be a dict when provided.")
        if self.cwd is not None and not isinstance(self.cwd, str):
            raise ValueError("LLM cwd must be a string when provided.")
        if self.cache_ttl is not None:
            if not isinstance(self.cache_ttl, int) or isinstance(self.cache_ttl, bool) or self.cache_ttl < 0:
                raise ValueError("LLM cache_ttl must be a non-negative integer when provided.")


class ProviderAdapter(ABC):
    provider_name: str
    tool_capable: bool = False

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


ADVISORY_LANES: frozenset[Lane] = frozenset({"verifier", "research", "judge"})

ADVISORY_SYSTEM_PROMPTS: dict[Lane, str] = {
    "verifier": (
        "You are the verifier lane. Work only from the supplied evidence. "
        "Identify risks, missing checks, contradictions, and whether the action should proceed."
    ),
    "research": (
        "You are the research synthesis lane. Work only from the supplied evidence. "
        "Summarize clearly, preserve uncertainty, and do not invent missing facts."
    ),
    "judge": (
        "You are the judge lane. Perform lightweight classification or scoring from the supplied evidence only. "
        "Be concise, explicit, and deterministic."
    ),
}


def build_effective_system_prompt(request: LLMRequest) -> str | None:
    default = ADVISORY_SYSTEM_PROMPTS.get(request.lane)
    if default and request.system_prompt:
        return f"{default}\n\n{request.system_prompt}"
    if default:
        return default
    return request.system_prompt


def build_effective_input(request: LLMRequest) -> UserPrompt:
    if request.lane not in ADVISORY_LANES:
        return request.prompt
    sections: list[str] = []
    if request.evidence_pack:
        sections.append(
            "# Evidence Pack\n"
            + json.dumps(request.evidence_pack, indent=2, sort_keys=True, default=str)
        )
    sections.append(f"# Task\n{request.prompt}")
    return "\n\n".join(sections)


def coerce_usage_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__"):
        return {
            key: item
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return {"value": str(value)}
