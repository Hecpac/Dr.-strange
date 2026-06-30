from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


VALID_MODEL_LANES = frozenset({"brain", "worker", "worker_heavy", "research", "verifier", "judge"})
VALID_MODEL_ROLES = frozenset(
    {
        "brain_primary",
        "computer_use_primary",
        "computer_use_fast",
        "browser_agent_primary",
        "browser_agent_fallback",
    }
)
LANE_ALIASES = {
    "coding": "worker",
    "code": "worker",
    "debug": "worker_heavy",
    "heavy": "worker_heavy",
    "terminal": "worker_heavy",
    "verify": "verifier",
    "verification": "verifier",
}
VALID_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


@dataclass(frozen=True, slots=True)
class ModelRef:
    provider: str
    model: str
    billing: str
    source: str
    tool_capable: bool
    notes: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"key": self.key}


@dataclass(frozen=True, slots=True)
class ModelOverride:
    provider: str
    model: str
    billing: str
    effort: str | None = None
    source: str = "session"

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"key": self.key}


@dataclass(frozen=True, slots=True)
class ModelRoleDefault:
    role: str
    provider: str
    model: str
    billing: str
    source: str
    tool_capable: bool
    notes: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"key": self.key}


class ModelRegistry:
    """Declarative model registry with explicit billing/source semantics."""

    PROVIDER_BILLING = {
        "anthropic": "claude_subscription_or_api",
        "codex": "chatgpt_subscription",
        "openai": "api",
        "google": "api",
        "ollama": "local",
    }
    TOOL_CAPABLE_PROVIDERS = frozenset({"anthropic", "codex", "openai"})

    DEFAULT_MODELS = (
        ("anthropic", "claude-opus-4-7", "Claude primary brain model"),
        ("anthropic", "claude-sonnet-4-6", "Claude worker/advisory model"),
        ("anthropic", "claude-haiku-4-5", "Claude browser fallback model"),
        ("codex", "codex-mini-latest", "Codex CLI via ChatGPT subscription"),
        ("codex", "gpt-5.3-codex", "Codex coding model via ChatGPT subscription"),
        ("codex", "gpt-5.5", "Only valid if your Codex CLI account exposes this model"),
        ("codex", "gpt-5.4-mini", "Fast Codex CLI model via ChatGPT subscription"),
        ("openai", "gpt-5.5", "OpenAI API billing, not ChatGPT subscription"),
        ("openai", "gpt-5.4", "OpenAI API billing"),
        ("openai", "gpt-5.4-mini", "OpenAI API billing"),
        ("google", "gemini-2.5-pro", "Google API billing"),
        ("ollama", "gemma4", "Local Ollama runtime"),
    )
    DEFAULT_MODEL_ROLES = (
        (
            "brain_primary",
            "anthropic",
            "claude-opus-4-7",
            "Primary reasoning brain. Keep separate from browser/computer executors.",
        ),
        (
            "computer_use_primary",
            "codex",
            "gpt-5.5",
            "Primary desktop automation model through Codex CLI and ChatGPT subscription.",
        ),
        (
            "computer_use_fast",
            "codex",
            "gpt-5.4-mini",
            "Fast desktop automation lane through Codex CLI and ChatGPT subscription.",
        ),
        (
            "browser_agent_primary",
            "anthropic",
            "claude-sonnet-4-6",
            "Browser-use planning model through Claude subscription/OAuth when configured.",
        ),
        (
            "browser_agent_fallback",
            "anthropic",
            "claude-haiku-4-5",
            "Secondary browser-use OAuth model for primary-model availability failures.",
        ),
    )

    def __init__(
        self,
        models: list[ModelRef] | None = None,
        model_roles: list[ModelRoleDefault] | None = None,
    ) -> None:
        self._models = {model.key: model for model in (models or self._default_models())}
        self._model_roles = {
            role.role: role for role in (model_roles or self._default_model_roles())
        }

    @classmethod
    def default(cls) -> "ModelRegistry":
        return cls()

    def list_models(self) -> list[ModelRef]:
        return sorted(self._models.values(), key=lambda item: (item.provider, item.model))

    def list_model_roles(self) -> list[ModelRoleDefault]:
        return sorted(self._model_roles.values(), key=lambda item: item.role)

    def resolve_role(self, role: str) -> ModelRoleDefault:
        normalized = str(role or "").strip().lower()
        if normalized not in VALID_MODEL_ROLES:
            raise ValueError(f"Role inválido: {role}")
        try:
            return self._model_roles[normalized]
        except KeyError as exc:
            raise ValueError(f"Role sin modelo configurado: {role}") from exc

    def resolve(self, value: str) -> ModelRef:
        provider, model = parse_model_selector(value)
        key = f"{provider}:{model}"
        if key in self._models:
            return self._models[key]
        if provider not in self.PROVIDER_BILLING:
            raise ValueError(f"Proveedor inválido: {provider}")
        if not model:
            raise ValueError("Modelo vacío")
        return ModelRef(
            provider=provider,
            model=model,
            billing=self.PROVIDER_BILLING[provider],
            source="dynamic",
            tool_capable=provider in self.TOOL_CAPABLE_PROVIDERS,
            notes="Modelo dinámico no listado; se enviará al provider indicado.",
        )

    def override_from_selector(self, selector: str, *, effort: str | None = None) -> ModelOverride:
        if effort is not None and effort not in VALID_EFFORTS:
            raise ValueError(f"Effort inválido: {effort}")
        ref = self.resolve(selector)
        return ModelOverride(
            provider=ref.provider,
            model=ref.model,
            billing=ref.billing,
            effort=effort,
        )

    @classmethod
    def _default_models(cls) -> list[ModelRef]:
        return [
            ModelRef(
                provider=provider,
                model=model,
                billing=cls.PROVIDER_BILLING[provider],
                source="registry",
                tool_capable=provider in cls.TOOL_CAPABLE_PROVIDERS,
                notes=notes,
            )
            for provider, model, notes in cls.DEFAULT_MODELS
        ]

    @classmethod
    def _default_model_roles(cls) -> list[ModelRoleDefault]:
        return [
            ModelRoleDefault(
                role=role,
                provider=provider,
                model=model,
                billing=cls.PROVIDER_BILLING[provider],
                source="registry",
                tool_capable=provider in cls.TOOL_CAPABLE_PROVIDERS,
                notes=notes,
            )
            for role, provider, model, notes in cls.DEFAULT_MODEL_ROLES
        ]


def parse_model_selector(value: str) -> tuple[str, str]:
    raw = value.strip()
    if not raw:
        raise ValueError("Modelo vacío")
    if ":" in raw:
        provider, model = raw.split(":", maxsplit=1)
        provider = _normalize_provider(provider)
        model = model.strip()
    else:
        provider, model = _infer_provider(raw), raw
    if provider == "subscription":
        provider = "codex"
    model = model.strip()
    if not model:
        raise ValueError("Modelo vacío")
    return provider, model


def normalize_model_lane(value: str) -> str:
    lane = LANE_ALIASES.get(value.strip().lower(), value.strip().lower())
    if lane not in VALID_MODEL_LANES:
        raise ValueError(f"Lane inválido: {value}")
    return lane


def model_overrides_from_state(state: dict[str, Any]) -> dict[str, ModelOverride]:
    active_object = state.get("active_object") or {}
    raw_overrides = active_object.get("model_overrides") or {}
    result: dict[str, ModelOverride] = {}
    if not isinstance(raw_overrides, dict):
        return result
    for lane, payload in raw_overrides.items():
        if not isinstance(payload, dict):
            continue
        provider = payload.get("provider")
        model = payload.get("model")
        billing = payload.get("billing")
        if (
            not isinstance(provider, str)
            or not isinstance(model, str)
            or not isinstance(billing, str)
        ):
            continue
        effort = payload.get("effort")
        result[str(lane)] = ModelOverride(
            provider=provider,
            model=model,
            billing=billing,
            effort=str(effort) if effort else None,
            source=str(payload.get("source") or "session"),
        )
    return result


def serialize_model_overrides(overrides: dict[str, ModelOverride]) -> dict[str, dict[str, Any]]:
    return {lane: override.to_dict() for lane, override in sorted(overrides.items())}


def _normalize_provider(value: str) -> str:
    provider = value.strip().lower()
    if provider == "chatgpt":
        return "codex"
    return provider


def _infer_provider(model: str) -> str:
    lowered = model.lower()
    if lowered.startswith("claude"):
        return "anthropic"
    if lowered.startswith("gemini"):
        return "google"
    if lowered.startswith("codex"):
        return "codex"
    if lowered.startswith("gpt") or lowered.startswith("o3") or lowered.startswith("o4"):
        return "openai"
    return "ollama"
