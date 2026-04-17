from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

from claw_v2.types import Lane, LLMResponse


class AdapterError(RuntimeError):
    """Base error for provider adapter failures."""


class AdapterUnavailableError(AdapterError):
    """Raised when a provider adapter cannot be used in the current environment."""


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


class ProviderAdapter(ABC):
    provider_name: str
    tool_capable: bool = False

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError


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
