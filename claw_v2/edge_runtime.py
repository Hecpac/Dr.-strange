from __future__ import annotations

from typing import Any

from claw_v2.edge_client import CoreEdgeClient


def make_edge_client(config: Any, *, observe: Any | None = None, jobs: Any | None = None) -> CoreEdgeClient | None:
    if not getattr(config, "edge_enabled", False):
        return None
    if not getattr(config, "edge_endpoint", None) or not getattr(config, "edge_secret", None):
        return None
    return CoreEdgeClient(
        endpoint=config.edge_endpoint,
        key_id=config.edge_key_id,
        secret=config.edge_secret,
        observe=observe,
        jobs=jobs,
    )


def apply_edge_health_to_report(config: Any, report: Any, edge_client: Any | None) -> None:
    if not getattr(config, "edge_enabled", False):
        return
    capabilities = list(getattr(config, "edge_capabilities", []) or [])
    if edge_client is None:
        _degrade_all(report, capabilities, "Edge client is not configured")
        return
    try:
        identity = edge_client.fetch_identity()
    except Exception as exc:
        _degrade_all(report, capabilities, f"Edge identity unavailable: {exc}")
        return
    report.add_ok("edge_identity", f"{identity.edge_id} via {identity.connectivity_layer}")
    health = edge_client.health()
    if not health.ready:
        _degrade_all(report, capabilities, health.reason or "Edge health is not ready")
        return
    for capability in capabilities:
        status = health.capabilities.get(capability, "unavailable")
        if status == "available":
            report.add_ok(f"edge_{capability}", "Edge capability available", capability=capability)
        else:
            reason = health.degraded_reasons.get(capability) or f"Edge capability is {status}"
            report.add_degraded(f"edge_{capability}", reason, capability=capability)


def _degrade_all(report: Any, capabilities: list[str], reason: str) -> None:
    for capability in capabilities:
        report.add_degraded(f"edge_{capability}", reason, capability=capability)
