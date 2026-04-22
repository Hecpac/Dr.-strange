from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.agents import FileAgentStore, SubAgentService
from claw_v2.bus import AgentBus, _new_message
from claw_v2.capability_registry import (
    CapabilityManifest,
    CapabilityRegistry,
    default_capability_registry,
    load_capability_manifest,
)
from claw_v2.coordinator import CoordinatorService, WorkerTask
from claw_v2.ecosystem import EcosystemHealthService


def test_default_registry_routes_by_inferred_capability() -> None:
    registry = default_capability_registry()

    assert registry.select(text="debug failing pytest", lane="worker").name == "hex"
    assert registry.select(text="cron deploy health incident", lane="research").name == "rook"
    assert registry.select(text="qa verify visual regression", lane="judge").name == "eval"
    assert registry.select(text="seo content campaign brief", lane="research").name == "lux"
    assert registry.select(text="calendar telegram personal reminder", lane="brain").name == "alma"


def test_manifest_loader_merges_declarative_file_and_skill_names(tmp_path: Path) -> None:
    agent_dir = tmp_path / "nova"
    agent_dir.mkdir()
    (agent_dir / "CAPABILITIES.json").write_text(
        """
        {
          "capabilities": ["research"],
          "domains": ["market"],
          "skills": ["briefing"],
          "lanes": ["research"],
          "risk_policy": {"publishing": "approval_required"}
        }
        """,
        encoding="utf-8",
    )

    manifest = load_capability_manifest(
        agent_dir,
        display_name="Nova",
        provider="openai",
        model="gpt-5.4",
        skills=["forecasting"],
    )

    assert manifest.name == "nova"
    assert manifest.provider == "openai"
    assert manifest.skills == ["briefing", "forecasting"]
    assert manifest.risk_policy["publishing"] == "approval_required"


def test_sub_agent_service_registry_includes_capability_metadata(tmp_path: Path) -> None:
    service = SubAgentService(Path("agents"), router=MagicMock(), store=FileAgentStore(tmp_path / "state"))

    discovered = service.discover()
    registry = service.registry()

    assert "lux" in discovered
    assert registry["hex"]["capabilities"] == ["coding", "debugging", "refactoring", "code_review"]
    assert "pytest" in registry["hex"]["tools"]
    assert registry["eval"]["risk_policy"]["evidence"] == "required"
    assert service.capability_registry().select(required=["marketing"]).name == "lux"


def test_coordinator_routes_unassigned_worker_by_required_capability(tmp_path: Path) -> None:
    manifest = CapabilityManifest(
        name="hex",
        display_name="Hex",
        provider="codex",
        model="codex-mini-latest",
        capabilities=["coding"],
        lanes=["worker"],
        soul_text="You are Hex.",
    )
    router = MagicMock()
    router.ask.return_value = MagicMock(content="patched")
    service = CoordinatorService(
        router=router,
        observe=MagicMock(),
        scratch_root=tmp_path,
        capability_registry=CapabilityRegistry([manifest]),
    )

    result = service._execute_worker(
        WorkerTask(
            name="fix-login",
            instruction="fix the login bug",
            lane="worker",
            required_capabilities=["coding"],
        )
    )

    assert result.content == "patched"
    call_kwargs = router.ask.call_args.kwargs
    assert call_kwargs["provider"] == "codex"
    assert call_kwargs["model"] == "codex-mini-latest"
    assert call_kwargs["system_prompt"] == "You are Hex."
    assert call_kwargs["evidence_pack"]["selected_agent"] == "hex"


def test_agent_bus_custom_inventory_drives_broadcast(tmp_path: Path) -> None:
    bus = AgentBus(bus_root=tmp_path, agent_names=["hex", "nova"])
    message = _new_message(from_agent="hex", to_agent=None, intent="notify", topic="ready", payload={})

    bus.send(message)

    assert len(list((tmp_path / "inbox" / "nova").glob("*.json"))) == 1
    assert len(list((tmp_path / "inbox" / "hex").glob("*.json"))) == 0
    assert not (tmp_path / "inbox" / "rook").exists()


def test_ecosystem_uses_registry_inventory_for_bus_lag() -> None:
    bus = MagicMock()
    bus.pending_count.side_effect = lambda name: {"hex": 2, "nova": 3}[name]
    service = EcosystemHealthService(
        bus=bus,
        observe=MagicMock(cost_per_agent_today=MagicMock(return_value={})),
        dream_states={},
        heartbeat=MagicMock(),
        agent_names=["hex", "nova"],
    )

    metric = service._check_bus_lag()

    assert metric.value == 5
    assert bus.pending_count.call_count == 2
