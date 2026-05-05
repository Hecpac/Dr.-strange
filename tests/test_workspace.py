from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from claw_v2.workspace import AgentWorkspace


class AgentWorkspaceTests(unittest.TestCase):
    def test_ensure_creates_required_workspace_files_without_overwriting_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "SOUL.md").write_text("# Custom Soul\n\nHuman edited.", encoding="utf-8")

            workspace = AgentWorkspace(root)
            result = workspace.ensure()

            self.assertIn("SOUL.md", result.existing_files)
            self.assertNotIn("SOUL.md", result.created_files)
            self.assertEqual((root / "SOUL.md").read_text(encoding="utf-8"), "# Custom Soul\n\nHuman edited.")
            for name in AgentWorkspace.REQUIRED_FILES:
                self.assertTrue((root / name).exists(), name)
            self.assertTrue((root / "memory").is_dir())

    def test_boot_protocol_is_required_critical_workspace_source(self) -> None:
        self.assertIn("BOOT_PROTOCOL.md", AgentWorkspace.REQUIRED_FILES)
        self.assertIn("BOOT_PROTOCOL.md", AgentWorkspace.STABLE_CONTEXT_FILES)
        self.assertLess(
            AgentWorkspace.STABLE_CONTEXT_FILES.index("BOOT_PROTOCOL.md"),
            AgentWorkspace.STABLE_CONTEXT_FILES.index("SOUL.md"),
        )

    def test_stable_context_loads_workspace_files_in_runtime_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = AgentWorkspace(root)
            workspace.ensure()
            (root / "MEMORY.md").write_text("# MEMORY.md\n\nPrefers concise updates.", encoding="utf-8")

            context = workspace.stable_context()

            self.assertIn("## SOUL.md", context)
            self.assertIn("## AGENTS.md", context)
            self.assertIn("## USER.md", context)
            self.assertIn("## BOOT_PROTOCOL.md", context)
            self.assertIn("## TOOLS.md", context)
            self.assertIn("Prefers concise updates.", context)
            self.assertLess(context.index("## SOUL.md"), context.index("## AGENTS.md"))

    def test_system_prompt_falls_back_when_workspace_has_no_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = AgentWorkspace(Path(tmpdir))

            self.assertEqual(workspace.system_prompt(fallback="fallback"), "fallback")

    def test_startup_context_contains_required_continuity_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = AgentWorkspace(root)
            workspace.ensure()
            (root / "BOOT_PROTOCOL.md").write_text(
                "# BOOT_PROTOCOL.md\n\n"
                "- Dr. Strange\n"
                "- Hector Pachano, fundador de Pachano Design\n"
                "- español natural\n"
                "- no respuestas-paja\n"
                "- contexto interno != respuesta externa\n"
                "- no asumir API/Pro/modelo/canal sin verificar\n"
                "- separación persona/modelo/runtime\n",
                encoding="utf-8",
            )
            (root / "SOUL.md").write_text("Dr. Strange persona principal.", encoding="utf-8")
            (root / "USER.md").write_text("Hector Pachano\nPachano Design", encoding="utf-8")
            (root / "MEMORY.md").write_text("# MEMORY.md\n\nmemoria persistente\n", encoding="utf-8")
            (root / "memory" / "2026-05-04.md").write_text(
                "# 2026-05-04\n\n- Decision tomada y tarea abierta.\n",
                encoding="utf-8",
            )
            config = _FakeConfig(root)
            memory = _FakeMemory(root / "data" / "claw.db")
            task_ledger = _FakeTaskLedger()

            context, report = workspace.startup_context(
                config=config,
                memory=memory,
                task_ledger=task_ledger,
                channel="telegram",
            )

            self.assertIn("Dr. Strange", context)
            self.assertIn("Hector", context)
            self.assertIn("Pachano Design", context)
            self.assertIn("español natural", context)
            self.assertIn("no respuestas-paja", context)
            self.assertIn("contexto interno != respuesta externa", context)
            self.assertIn("memoria persistente", context)
            self.assertIn("no asumir API/Pro/modelo/canal sin verificar", context)
            self.assertIn("separación persona/modelo/runtime", context)
            self.assertIn("task_ledger=loaded", context)
            self.assertIn("open_tasks:", context)
            self.assertIn("boot_protocol_loaded=true", context)
            self.assertIn("boot_context_version=startup_context_v2", context)
            self.assertIn("startup_context_used=true", context)
            self.assertIn("stable_context_used=false", context)
            self.assertIn("memory/2026-05-04.md", context)
            self.assertTrue(report.boot_protocol_loaded)
            self.assertTrue(report.task_ledger_loaded)
            self.assertTrue(report.configuration_loaded)
            self.assertTrue(report.startup_context_used)
            self.assertFalse(report.stable_context_used)
            self.assertTrue(report.daily_memory_loaded)
            self.assertIn("BOOT_PROTOCOL.md", report.loaded_files)
            self.assertIn("2026-05-04.md", report.daily_memory_files)
            self.assertNotIn("corro en este CLI", context)

    def test_startup_context_reports_missing_boot_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = AgentWorkspace(root)
            workspace.ensure()
            (root / "BOOT_PROTOCOL.md").unlink()

            context, report = workspace.startup_context()

            self.assertIn("boot_protocol_loaded=false", context)
            self.assertFalse(report.boot_protocol_loaded)
            self.assertIn("BOOT_PROTOCOL.md", report.missing_files)
            self.assertTrue(any(source.name == "BOOT_PROTOCOL.md" and source.status == "missing" for source in report.attempted_sources))

class _FakeConfig:
    telegram_bot_token = "configured-token"
    telegram_allowed_user_id = "123"
    web_chat_enabled = True
    web_chat_host = "127.0.0.1"
    web_chat_port = 8765
    browse_backend = "auto"
    chrome_cdp_enabled = True
    computer_use_enabled = True
    claude_auth_mode = "subscription"
    runtime_config_path = None

    def __init__(self, root: Path) -> None:
        self.workspace_root = root
        self.db_path = root / "data" / "claw.db"
        self.telemetry_root = root / "telemetry"
        self.agent_state_root = root / "agents"

    def provider_for_lane(self, lane: str) -> str:
        return "anthropic" if lane in {"brain", "research"} else "codex"

    def model_for_lane(self, lane: str) -> str:
        return "claude-opus-4-7" if lane == "brain" else "gpt-5.4"

    def effort_for_lane(self, lane: str) -> str:
        return "high"


class _FakeMemory:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def get_profile_facts(self) -> list[dict]:
        return [
            {
                "key": "profile.user",
                "value": "Hector Pachano, founder of Pachano Design; prefers español natural.",
            }
        ]

    def get_learning_facts(self, limit: int = 5) -> list[dict]:
        return [{"key": "learning_loop_consolidated", "value": "no respuestas-paja"}]

    def list_session_states(self, *, limit: int = 5) -> list[dict]:
        return [
            {
                "session_id": "tg-123",
                "autonomy_mode": "assisted",
                "mode": "chat",
                "current_goal": "mantener continuidad",
                "pending_action": "verificar boot",
                "verification_status": "unknown",
                "task_queue": [{"task": "boot"}],
                "active_object_keys": ["model_overrides"],
            }
        ]


class _FakeTaskLedger:
    def summary(self) -> dict[str, int]:
        return {"running": 1}

    def list(self, *, statuses=None, limit: int = 20) -> list[SimpleNamespace]:
        running = SimpleNamespace(
            task_id="task-boot",
            session_id="tg-123",
            status="running",
            verification_status="unknown",
            objective="validar startup context",
        )
        failed = SimpleNamespace(
            task_id="task-old",
            session_id="tg-123",
            status="failed",
            verification_status="missing_evidence",
            objective="tarea previa sin evidencia",
        )
        if statuses is not None:
            return [running]
        return [running, failed]


if __name__ == "__main__":
    unittest.main()
