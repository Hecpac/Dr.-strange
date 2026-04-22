from __future__ import annotations

from pathlib import Path

from claw_v2.edge_protocol import EdgeHealth, EdgeIdentity
from claw_v2.edge_runtime import apply_edge_health_to_report, make_edge_client
from claw_v2.main import StartupHealthReport, _run_startup_healthchecks
from claw_v2.observe import ObserveStream

from tests.helpers import make_config


class FakeEdgeClient:
    def __init__(self, *, identity: EdgeIdentity | Exception, health: EdgeHealth | None = None) -> None:
        self.identity = identity
        self._health = health or EdgeHealth.unavailable("mac asleep")

    def fetch_identity(self) -> EdgeIdentity:
        if isinstance(self.identity, Exception):
            raise self.identity
        return self.identity

    def health(self) -> EdgeHealth:
        return self._health


def _edge_config(tmp_path: Path):
    config = make_config(tmp_path)
    config.edge_enabled = True
    config.edge_endpoint = "https://mac.tailnet.ts.net"
    config.edge_key_id = "core"
    config.edge_secret = "secret"
    config.edge_capabilities = ["computer_use", "chrome_cdp"]
    return config


def _identity() -> EdgeIdentity:
    return EdgeIdentity(
        edge_id="mac-edge",
        endpoint="https://mac.tailnet.ts.net",
        capabilities=["computer_use", "chrome_cdp"],
        key_id="edge",
        connectivity_layer="tailscale",
    )


def test_make_edge_client_is_disabled_by_default(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    assert make_edge_client(config) is None


def test_edge_health_not_ready_degrades_all_edge_capabilities(tmp_path: Path) -> None:
    config = _edge_config(tmp_path)
    report = StartupHealthReport()

    apply_edge_health_to_report(config, report, FakeEdgeClient(identity=_identity(), health=EdgeHealth.unavailable("mac asleep")))

    degraded = report.degraded_capabilities()
    assert degraded["computer_use"] == "mac asleep"
    assert degraded["chrome_cdp"] == "mac asleep"


def test_edge_health_partial_capability_degrades_only_unavailable_capability(tmp_path: Path) -> None:
    config = _edge_config(tmp_path)
    report = StartupHealthReport()
    health = EdgeHealth(
        ready=True,
        capabilities={"computer_use": "available", "chrome_cdp": "degraded"},
        degraded_reasons={"chrome_cdp": "Chrome profile locked"},
    )

    apply_edge_health_to_report(config, report, FakeEdgeClient(identity=_identity(), health=health))

    degraded = report.degraded_capabilities()
    assert "computer_use" not in degraded
    assert degraded["chrome_cdp"] == "Chrome profile locked"
    assert any(item.name == "edge_identity" for item in report.ok)
    assert any(item.name == "edge_computer_use" for item in report.ok)


def test_edge_identity_failure_degrades_all_edge_capabilities(tmp_path: Path) -> None:
    config = _edge_config(tmp_path)
    report = StartupHealthReport()

    apply_edge_health_to_report(config, report, FakeEdgeClient(identity=RuntimeError("auth failed")))

    degraded = report.degraded_capabilities()
    assert "Edge identity unavailable" in degraded["computer_use"]
    assert "auth failed" in degraded["chrome_cdp"]


def test_startup_healthchecks_apply_edge_degraded_capabilities(tmp_path: Path) -> None:
    config = _edge_config(tmp_path)
    config.eval_on_self_improve = False
    config.chrome_cdp_enabled = False
    observe = ObserveStream(tmp_path / "claw.db")

    report = _run_startup_healthchecks(
        config,
        observe,
        edge_client=FakeEdgeClient(identity=_identity(), health=EdgeHealth.unavailable("tunnel offline")),
    )

    degraded = report.degraded_capabilities()
    assert degraded["computer_use"] == "tunnel offline"
    assert degraded["chrome_cdp"] == "tunnel offline"


def test_startup_healthchecks_use_edge_as_source_for_edge_owned_capabilities(tmp_path: Path) -> None:
    config = _edge_config(tmp_path)
    config.edge_capabilities = ["computer_use", "computer_control", "chrome_cdp", "browser_use"]
    config.eval_on_self_improve = False
    config.chrome_cdp_enabled = False
    config.computer_use_enabled = False
    observe = ObserveStream(tmp_path / "claw.db")
    health = EdgeHealth(
        ready=True,
        capabilities={capability: "available" for capability in config.edge_capabilities},
    )

    report = _run_startup_healthchecks(config, observe, edge_client=FakeEdgeClient(identity=_identity(), health=health))

    degraded = report.degraded_capabilities()
    for capability in config.edge_capabilities:
        assert capability not in degraded
        assert any(item.name == f"edge_{capability}" for item in report.ok)
