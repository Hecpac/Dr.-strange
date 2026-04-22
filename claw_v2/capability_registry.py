from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass(slots=True)
class CapabilityManifest:
    name: str
    display_name: str
    provider: str
    model: str
    capabilities: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    risk_policy: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    sla: dict[str, Any] = field(default_factory=dict)
    lanes: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    soul_text: str = ""

    @classmethod
    def from_mapping(cls, name: str, data: dict[str, Any]) -> "CapabilityManifest":
        return cls(
            name=str(data.get("name") or name),
            display_name=str(data.get("display_name") or data.get("name") or name),
            provider=str(data.get("provider") or "anthropic"),
            model=str(data.get("model") or "claude-sonnet-4-6"),
            capabilities=_as_list(data.get("capabilities")),
            domains=_as_list(data.get("domains")),
            tools=_as_list(data.get("tools")),
            skills=_as_list(data.get("skills")),
            risk_policy=dict(data.get("risk_policy") or {}),
            budget=dict(data.get("budget") or {}),
            sla=dict(data.get("sla") or {}),
            lanes=_as_list(data.get("lanes")) or ["research"],
            tags=_as_list(data.get("tags")),
            soul_text=str(data.get("soul_text") or ""),
        )

    def to_router_entry(self, soul_text: str | None = None) -> dict[str, Any]:
        return {
            "display_name": self.display_name,
            "provider": self.provider,
            "model": self.model,
            "soul_text": self.soul_text if soul_text is None else soul_text,
            "capabilities": self.capabilities,
            "domains": self.domains,
            "tools": self.tools,
            "skills": self.skills,
            "risk_policy": self.risk_policy,
            "budget": self.budget,
            "sla": self.sla,
            "lanes": self.lanes,
            "tags": self.tags,
        }

    def score(self, *, required: Iterable[str], lane: str | None, text: str) -> int:
        terms = self._terms()
        required_terms = [_normalize(item) for item in required if item]
        if required_terms and not all(item in terms for item in required_terms):
            return -1
        score = 10 * len(required_terms)
        if lane and _normalize(lane) in {_normalize(item) for item in self.lanes}:
            score += 3
        score += sum(1 for token in _tokenize(text) if token in terms)
        return score

    def _terms(self) -> set[str]:
        values = [
            self.name,
            self.display_name,
            *self.capabilities,
            *self.domains,
            *self.tools,
            *self.skills,
            *self.lanes,
            *self.tags,
        ]
        terms: set[str] = set()
        for value in values:
            normalized = _normalize(value)
            terms.add(normalized)
            terms.update(_tokenize(value))
        return terms


class CapabilityRegistry:
    def __init__(self, manifests: Iterable[CapabilityManifest] = ()) -> None:
        self._by_name = {manifest.name: manifest for manifest in manifests}

    @classmethod
    def from_mapping(cls, registry: dict[str, dict[str, Any]] | None) -> "CapabilityRegistry":
        if not registry:
            return cls()
        return cls(CapabilityManifest.from_mapping(name, data) for name, data in registry.items())

    def register(self, manifest: CapabilityManifest) -> None:
        self._by_name[manifest.name] = manifest

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def get(self, name: str) -> CapabilityManifest | None:
        return self._by_name.get(name)

    def as_router_registry(self) -> dict[str, dict[str, Any]]:
        return {name: manifest.to_router_entry() for name, manifest in self._by_name.items()}

    def select(self, *, required: Iterable[str] = (), lane: str | None = None, text: str = "") -> CapabilityManifest | None:
        required_terms = list(required)
        inferred_terms = [] if required_terms else infer_capabilities(text)
        text_terms = _tokenize(text)
        scored = []
        for manifest in self._by_name.values():
            terms = manifest._terms()
            score = manifest.score(required=required_terms, lane=lane, text=text)
            if not required_terms and not inferred_terms and not (text_terms & terms):
                score = -1
            if inferred_terms:
                score += 10 * sum(1 for item in inferred_terms if _normalize(item) in terms)
            scored.append((score, manifest.name, manifest))
        scored = [item for item in scored if item[0] > 0]
        if not scored:
            return None
        return max(scored, key=lambda item: (item[0], item[1]))[2]

    def describe_for_prompt(self) -> str:
        lines = []
        for manifest in sorted(self._by_name.values(), key=lambda item: item.name):
            lines.append(
                f"- {manifest.name}: capabilities={manifest.capabilities}, "
                f"domains={manifest.domains}, lanes={manifest.lanes}, skills={manifest.skills}"
            )
        return "\n".join(lines)


def load_capability_manifest(
    agent_dir: Path,
    *,
    display_name: str,
    provider: str,
    model: str,
    skills: Iterable[str],
    soul_text: str = "",
) -> CapabilityManifest:
    data = dict(_DEFAULT_MANIFESTS.get(agent_dir.name, {}))
    manifest_path = agent_dir / "CAPABILITIES.json"
    if manifest_path.exists():
        data.update(json.loads(manifest_path.read_text(encoding="utf-8")))
    data.setdefault("name", agent_dir.name)
    data.setdefault("display_name", display_name)
    data.setdefault("provider", provider)
    data.setdefault("model", model)
    data["skills"] = sorted(set(_as_list(data.get("skills"))) | set(skills))
    data["soul_text"] = soul_text
    return CapabilityManifest.from_mapping(agent_dir.name, data)


def default_capability_registry() -> CapabilityRegistry:
    return CapabilityRegistry.from_mapping(_DEFAULT_MANIFESTS)


def default_agent_names() -> tuple[str, ...]:
    return tuple(default_capability_registry().names())


def infer_capabilities(text: str) -> list[str]:
    tokens = set(_tokenize(text))
    inferred: list[str] = []
    for capability, keywords in _INFERENCE_RULES.items():
        if tokens & keywords:
            inferred.append(capability)
    return inferred


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        return sorted(str(item) for item in value)
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _tokenize(value: str) -> set[str]:
    return {_normalize(item) for item in re.findall(r"[A-Za-z0-9]+", value) if item}


_INFERENCE_RULES: dict[str, set[str]] = {
    "coding": {"code", "bug", "debug", "refactor", "pr", "tests", "pytest", "merge"},
    "operations": {"ops", "deploy", "health", "logs", "cron", "server", "incident"},
    "evaluation": {"eval", "qa", "verify", "grade", "screenshot", "regression"},
    "marketing": {"marketing", "seo", "content", "campaign", "ads", "newsletter"},
    "personal_assistant": {"calendar", "telegram", "message", "brief", "personal", "reminder"},
}


_DEFAULT_MANIFESTS: dict[str, dict[str, Any]] = {
    "alma": {
        "display_name": "Alma", "provider": "anthropic", "model": "claude-opus-4-7",
        "capabilities": ["personal_assistant", "synthesis", "daily_brief"],
        "domains": ["personal", "calendar", "telegram", "memory"],
        "tools": ["memory", "calendar", "telegram"],
        "lanes": ["research", "brain"],
        "tags": ["assistant", "brief", "message"],
    },
    "eval": {
        "display_name": "Eval", "provider": "anthropic", "model": "claude-sonnet-4-6",
        "capabilities": ["evaluation", "qa", "visual_review", "regression_testing"],
        "domains": ["quality", "browser", "accessibility", "testing"],
        "tools": ["playwright", "browser_use", "screenshots"],
        "lanes": ["research", "judge", "verifier"],
        "tags": ["qa", "verify", "grade"],
    },
    "hex": {
        "display_name": "Hex", "provider": "codex", "model": "codex-mini-latest",
        "capabilities": ["coding", "debugging", "refactoring", "code_review"],
        "domains": ["code", "architecture", "tests"],
        "tools": ["git", "filesystem", "pytest", "terminal"],
        "lanes": ["worker", "research"],
        "tags": ["code", "bug", "pr", "refactor"],
    },
    "lux": {
        "display_name": "Lux", "provider": "openai", "model": "gpt-5.4",
        "capabilities": ["marketing", "seo", "content_strategy", "campaign_analysis"],
        "domains": ["marketing", "content", "seo", "ads"],
        "tools": ["analytics", "web_research"],
        "lanes": ["research"],
        "tags": ["content", "campaign", "newsletter"],
    },
    "rook": {
        "display_name": "Rook", "provider": "anthropic", "model": "claude-sonnet-4-6",
        "capabilities": ["operations", "incident_response", "log_analysis", "health_audit"],
        "domains": ["ops", "deploy", "security", "cron"],
        "tools": ["logs", "terminal", "healthcheck"],
        "lanes": ["research", "worker"],
        "tags": ["ops", "deploy", "incident", "cron"],
    },
}
