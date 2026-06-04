from __future__ import annotations

from typing import Any

from claw_v2.types import ProviderRole


def router_timeout_for_role(router: Any, role: ProviderRole, *, default: float) -> float:
    config = getattr(router, "config", None)
    timeout_for_role = getattr(config, "timeout_for_role", None)
    if callable(timeout_for_role):
        try:
            value = timeout_for_role(role)
            if isinstance(value, (int, float)):
                return float(value)
        except Exception:
            return default
    return default
