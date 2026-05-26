"""F3b.1 — Pluggable runner for external_check + preflight observations.

This module defines two registries (default empty) where tools or tests can
plug callable providers that return MOCKED observations. The runtime
`attach_artifact_to_result()` calls into these registries when a tool has a
declared external_check / preflight; if no provider is registered the
observation stays `None` and the gate falls through to its standard
pending_verification / blocked outcome.

NO live HTTP / CDP. The default registry is empty. Production wiring of
real HeyGen / Telegram / OpenAI fetchers is F3b.2+. Tests register fixture
providers via `register_external_observation_provider()` and
`register_preflight_provider()` for the duration of a test.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping


# A provider takes (tool_name, args, tool_result) and returns either:
#   - dict observation that gets stored as artifact["external_observation"]
#   - None when the observation cannot be determined (the gate then falls
#     through to pending_verification)
ExternalObservationProvider = Callable[
    [str, Mapping[str, Any], Mapping[str, Any]], dict[str, Any] | None
]

# Preflight providers return (passed: bool, reason: str). reason is recorded
# in the artifact for audit.
PreflightProvider = Callable[[str, Mapping[str, Any]], tuple[bool, str]]


_EXTERNAL_OBSERVATION_PROVIDERS: dict[str, ExternalObservationProvider] = {}
_PREFLIGHT_PROVIDERS: dict[str, PreflightProvider] = {}


def register_external_observation_provider(
    tool_name: str, provider: ExternalObservationProvider
) -> None:
    _EXTERNAL_OBSERVATION_PROVIDERS[tool_name] = provider


def register_preflight_provider(tool_name: str, provider: PreflightProvider) -> None:
    _PREFLIGHT_PROVIDERS[tool_name] = provider


def clear_providers() -> None:
    """Used by tests to ensure clean slate between cases."""
    _EXTERNAL_OBSERVATION_PROVIDERS.clear()
    _PREFLIGHT_PROVIDERS.clear()


def run_external_observation(
    tool_name: str,
    args: Mapping[str, Any],
    tool_result: Mapping[str, Any],
) -> dict[str, Any] | None:
    provider = _EXTERNAL_OBSERVATION_PROVIDERS.get(tool_name)
    if provider is None:
        return None
    try:
        result = provider(tool_name, args, tool_result)
    except Exception:
        return None
    return result if isinstance(result, dict) else None


def run_preflight(tool_name: str, args: Mapping[str, Any]) -> tuple[bool, str]:
    """Returns (passed, reason). Default if no provider: (False, "no_provider")."""
    provider = _PREFLIGHT_PROVIDERS.get(tool_name)
    if provider is None:
        return False, "no_provider"
    try:
        passed, reason = provider(tool_name, args)
    except Exception as exc:
        return False, f"provider_exception:{type(exc).__name__}"
    return bool(passed), str(reason or "")
