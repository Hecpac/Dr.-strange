from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from claw_v2.redaction import redact_sensitive
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
            self.assertIsNotNone(report.prompt_manifest)
            payload = report.to_dict()
            self.assertIsNotNone(payload["prompt_manifest"])
            self.assertEqual(payload["prompt_manifest"]["mode"], "shadow")
            self.assertGreater(len(payload["prompt_manifest"]["blocks"]), 0)

    def test_startup_context_reports_dirty_git_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            workspace = AgentWorkspace(root)
            workspace.ensure()

            context, report = workspace.startup_context()

            self.assertTrue(report.git_dirty)
            self.assertGreater(len(report.git_status_summary), 0)
            self.assertIn("git_dirty=true", context)
            self.assertIn("git_status_entries=", context)
            payload = report.to_dict()
            self.assertTrue(payload["git_dirty"])
            self.assertGreater(len(payload["git_status_summary"]), 0)

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
            self.assertIsNotNone(report.prompt_manifest)

    def test_prompt_manifest_shadow_does_not_change_context_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = AgentWorkspace(root)
            workspace.ensure()
            context, report = workspace.startup_context(channel="telegram")

            expected = _legacy_startup_context(root, report=report, channel="telegram")

            self.assertEqual(context, expected)
            self.assertIn("# Startup Context\n\nboot_context_version=", context)
            self.assertIsNotNone(report.prompt_manifest)
            self.assertEqual(report.prompt_manifest.mode, "shadow")
            self.assertEqual(report.prompt_manifest.total_included_chars, len(context))

    def test_prompt_manifest_includes_stable_block_hash_trust_source_priority_and_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = AgentWorkspace(root)
            workspace.ensure()

            _, report = workspace.startup_context()

            self.assertIsNotNone(report.prompt_manifest)
            blocks = {block.source: block for block in report.prompt_manifest.blocks}
            boot = blocks["BOOT_PROTOCOL.md"]
            self.assertEqual(boot.trust, "system")
            self.assertEqual(boot.priority, 100)
            self.assertEqual(boot.budget_chars, 30_000)
            self.assertRegex(boot.sha256, r"^[0-9a-f]{64}$")
            self.assertGreater(boot.actual_chars, 0)
            self.assertGreater(boot.included_chars, 0)

    def test_prompt_manifest_redacts_before_hashing_and_report_serialization(self) -> None:
        secret = "sk-secret-value-12345678901234567890"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = AgentWorkspace(root)
            workspace.ensure()
            (root / "MEMORY.md").write_text(
                f"# MEMORY.md\n\napi_key={secret}\n",
                encoding="utf-8",
            )

            context, report = workspace.startup_context()

            self.assertIn(secret, context)
            self.assertIsNotNone(report.prompt_manifest)
            memory_block = next(block for block in report.prompt_manifest.blocks if block.source == "MEMORY.md")
            self.assertTrue(memory_block.redacted)
            self.assertEqual(memory_block.actual_chars, memory_block.included_chars)
            self.assertEqual(report.prompt_manifest.total_included_chars, len(str(redact_sensitive(context, limit=0))))
            self.assertLess(report.prompt_manifest.total_included_chars, len(context))
            serialized = json.dumps(report.to_dict())
            self.assertNotIn(secret, serialized)
            self.assertNotIn("sk-secret-value", serialized)

    def test_prompt_manifest_shadow_diff_payload_contains_metrics_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = AgentWorkspace(root)
            workspace.ensure()
            context, report = workspace.startup_context()

            self.assertIsNotNone(report.prompt_manifest)
            payload = report.prompt_manifest.shadow_diff_payload(
                context_truncated=report.context_truncated,
            )

            self.assertEqual(payload["mode"], "shadow")
            self.assertEqual(payload["context_chars_redacted"], len(context))
            self.assertTrue(payload["shadow_context_unchanged"])
            self.assertIn("block_count", payload)


def _legacy_startup_context(root: Path, *, report, channel: str) -> str:
    today = datetime_from_iso(report.timestamp)
    sections = [
        "# Startup Context",
        "boot_context_version=startup_context_v2",
        "startup_context_used=true",
        "stable_context_used=false",
        f"startup_date={today.strftime('%Y-%m-%d')}",
        f"startup_weekday={today.strftime('%A')}",
        f"startup_channel={channel}",
        f"workspace_root={root}",
        f"cwd={report.cwd}",
        f"pid={report.pid}",
        f"code_version={report.code_version or 'unknown'}",
        f"git_dirty={str(report.git_dirty).lower()}",
        f"git_status_entries={len(report.git_status_summary)}",
        f"boot_protocol_loaded={str(report.boot_protocol_loaded).lower()}",
        f"boot_protocol_version={report.boot_protocol_version or 'unknown'}",
        "memoria persistente=required",
        "task_ledger=required",
        "regla: no asumir API/Pro/modelo/canal sin verificar.",
        "regla: separación persona/modelo/runtime; Dr. Strange es la persona, modelo/runtime/CLI/API/daemon son capas tecnicas.",
        "regla: contexto interno != respuesta externa; reportar fuentes/estado sin imprimir contenido privado completo.",
        "regla: Telegram es canal Telegram cuando current_channel=telegram; no describir Telegram como canal CLI salvo evidencia real de canal CLI.",
    ]
    for name in AgentWorkspace.STABLE_CONTEXT_FILES:
        path = root / name
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8").strip()
        if len(content) > 30_000:
            content = content[:30_000] + "\n\n[... truncated]"
        if content:
            sections.append(f"## {name}\n{content}")
    memory_dir = root / "memory"
    if memory_dir.exists():
        files = sorted(memory_dir.glob("20??-??-??.md"), reverse=True)[:5]
        if not files:
            sections.append("# Daily Working Notes\nNo dated memory files found.")
    sections.append("# Task Ledger Startup Snapshot\ntask_ledger=unavailable")
    context = "# Agent Workspace Context\n\n" + "\n\n".join(sections)
    if len(context) > 180_000:
        context = context[:180_000] + "\n\n[... startup context truncated]"
    return context


def datetime_from_iso(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)

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
