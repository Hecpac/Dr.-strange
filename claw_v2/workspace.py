from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from claw_v2.memory_retention import (
    classify_memory_fact,
    format_memory_fact_for_prompt,
)
from claw_v2.prompt_manifest import (
    PromptBlock,
    PromptManifest,
    PromptTrust,
    make_prompt_block,
    prompt_capsule_mode_from_env,
)
from claw_v2.redaction import redact_sensitive


_MAX_CONTEXT_CHARS_PER_FILE = 30_000
_MAX_DAILY_CONTEXT_CHARS_PER_FILE = 12_000
_MAX_STARTUP_CONTEXT_CHARS = 180_000
_MAX_STARTUP_FIELD_CHARS = 420
BOOT_CONTEXT_VERSION = "startup_context_v2"
_CONTEXT_PREFIX = "# Agent Workspace Context\n\n"
_CONTEXT_TRUNCATED_SUFFIX = "\n\n[... startup context truncated]"


_DEFAULT_FILES: dict[str, str] = {
    "AGENTS.md": """# AGENTS.md - Operating Instructions

This workspace is the agent's home.

## Runtime Contract
- Treat Telegram, web chat, cron, and CLI as channels, not as the agent identity.
- Use task records and session state for durable work instead of relying on chat history.
- Execute authorized work autonomously, verify outcomes, then report concise results.
- If blocked, record the blocker and the next concrete action.

## Memory
- Use MEMORY.md for durable facts, preferences, and decisions.
- Use memory/YYYY-MM-DD.md for daily working notes.
- Do not store secrets in memory files.
""",
    "TOOLS.md": """# TOOLS.md - Local Tool Notes

This file records local tool conventions for the agent.

## Defaults
- Prefer semantic runtime tools before shell escape hatches.
- Use absolute paths for file operations.
- Verify command results before reporting success.
""",
    "IDENTITY.md": """# IDENTITY.md

Name: Dr. Strange
Role: Autonomous personal agent for Hector Pachano
Primary language: Spanish

Always identify as "Dr. Strange" in chat. Never identify as Claude, Claude Code,
Anthropic CLI, "the model", or "the bot". If asked about the underlying model,
inspect the active runtime/configuration first and answer from verified evidence.
""",
    "MEMORY.md": """# MEMORY.md

Durable memories, preferences, and decisions belong here.
Keep entries concise and evidence-backed.
""",
    "BOOT_PROTOCOL.md": """# BOOT_PROTOCOL.md

Mandatory startup protocol for Dr. Strange.

- Identity: Dr. Strange, autonomous personal agent for Hector Pachano.
- User: Hector Pachano, founder of Pachano Design.
- Default language: español natural.
- Default style: directo, util, no respuestas-paja.
- Do not assume API/Pro/model/channel/path/permission state without local evidence.
- Keep persona, model, runtime, CLI, daemon, API, Telegram, and web chat as separate layers.
- Load persistent memory, dated working notes, session state, and task_ledger before answering.
- If context loading fails, log the failed source clearly without exposing secrets.
""",
    "BOOT.md": """# BOOT.md

Startup checklist for runtime restarts.

- Load BOOT_PROTOCOL.md first.
- Load identity, user profile, persistent memory, dated notes, session state, lessons, and task_ledger.
- Verify operational configuration before describing model/API/subscription/channel/path/permission state.
- Confirm workspace files are present.
- Confirm task/session state is durable before starting new work.
- Do not send outbound messages unless there is an actionable alert.
""",
}


@dataclass(slots=True)
class WorkspaceBootstrapResult:
    root: Path
    created_files: list[str] = field(default_factory=list)
    existing_files: list[str] = field(default_factory=list)
    memory_dir_created: bool = False

    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "created_files": list(self.created_files),
            "existing_files": list(self.existing_files),
            "memory_dir_created": self.memory_dir_created,
        }


@dataclass(slots=True)
class ContextSourceStatus:
    name: str
    path: str
    status: str
    chars: int = 0
    truncated: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "path": self.path,
            "status": self.status,
            "chars": self.chars,
            "truncated": self.truncated,
        }
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(slots=True)
class _PromptSectionDraft:
    block_id: str
    title: str
    source: str
    trust: PromptTrust
    priority: int
    budget_chars: int
    text: str
    truncated: bool = False


@dataclass(slots=True)
class StartupContextReport:
    root: str
    channel: str
    workspace_root: str = ""
    cwd: str = ""
    pid: int = 0
    timestamp: str = ""
    code_version: str = ""
    git_dirty: bool = False
    git_status_summary: list[str] = field(default_factory=list)
    boot_context_version: str = BOOT_CONTEXT_VERSION
    boot_protocol_version: str = ""
    startup_context_used: bool = True
    stable_context_used: bool = False
    attempted_sources: list[ContextSourceStatus] = field(default_factory=list)
    loaded_files: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    truncated_files: list[str] = field(default_factory=list)
    context_chars: int = 0
    context_truncated: bool = False
    boot_protocol_loaded: bool = False
    daily_memory_files: list[str] = field(default_factory=list)
    daily_memory_loaded: bool = False
    configuration_loaded: bool = False
    active_channels: list[str] = field(default_factory=list)
    task_ledger_loaded: bool = False
    task_ledger_counts: dict[str, int] = field(default_factory=dict)
    task_ledger_open_count: int = 0
    task_ledger_attention_count: int = 0
    session_state_loaded: bool = False
    session_state_count: int = 0
    learning_loaded: bool = False
    learning_count: int = 0
    memory_retention_counts: dict[str, int] = field(default_factory=dict)
    memory_retrieval_omitted_count: int = 0
    memory_never_prompt_count: int = 0
    prompt_manifest: PromptManifest | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "workspace_root": self.workspace_root or self.root,
            "cwd": self.cwd,
            "pid": self.pid,
            "timestamp": self.timestamp,
            "code_version": self.code_version,
            "git_dirty": self.git_dirty,
            "git_status_summary": list(self.git_status_summary),
            "boot_context_version": self.boot_context_version,
            "boot_protocol_version": self.boot_protocol_version,
            "startup_context_used": self.startup_context_used,
            "stable_context_used": self.stable_context_used,
            "channel": self.channel,
            "attempted_sources": [source.to_dict() for source in self.attempted_sources],
            "loaded_files": list(self.loaded_files),
            "missing_files": list(self.missing_files),
            "truncated_files": list(self.truncated_files),
            "context_chars": self.context_chars,
            "context_truncated": self.context_truncated,
            "boot_protocol_loaded": self.boot_protocol_loaded,
            "daily_memory_files": list(self.daily_memory_files),
            "daily_memory_loaded": self.daily_memory_loaded,
            "configuration_loaded": self.configuration_loaded,
            "active_channels": list(self.active_channels),
            "task_ledger_loaded": self.task_ledger_loaded,
            "task_ledger_counts": dict(self.task_ledger_counts),
            "task_ledger_open_count": self.task_ledger_open_count,
            "task_ledger_attention_count": self.task_ledger_attention_count,
            "session_state_loaded": self.session_state_loaded,
            "session_state_count": self.session_state_count,
            "learning_loaded": self.learning_loaded,
            "learning_count": self.learning_count,
            "memory_retention_counts": dict(self.memory_retention_counts),
            "memory_retrieval_omitted_count": self.memory_retrieval_omitted_count,
            "memory_never_prompt_count": self.memory_never_prompt_count,
            "prompt_manifest": self.prompt_manifest.to_dict() if self.prompt_manifest else None,
        }


class AgentWorkspace:
    """Workspace-first context and memory bootstrap for the agent runtime."""

    STABLE_CONTEXT_FILES = (
        "BOOT_PROTOCOL.md",
        "SOUL.md",
        "IDENTITY.md",
        "USER.md",
        "AGENTS.md",
        "CLAUDE.md",
        "BOOT.md",
        "HEARTBEAT.md",
        "TOOLS.md",
        "MEMORY.md",
    )

    REQUIRED_FILES = (
        "AGENTS.md",
        "SOUL.md",
        "TOOLS.md",
        "USER.md",
        "IDENTITY.md",
        "MEMORY.md",
        "HEARTBEAT.md",
        "BOOT_PROTOCOL.md",
        "BOOT.md",
    )

    def __init__(self, root: Path | str, *, template_root: Path | str | None = None) -> None:
        self.root = Path(root)
        self.template_root = (
            Path(template_root) if template_root is not None else Path(__file__).parent
        )

    def ensure(self) -> WorkspaceBootstrapResult:
        self.root.mkdir(parents=True, exist_ok=True)
        created: list[str] = []
        existing: list[str] = []
        for name in self.REQUIRED_FILES:
            target = self.root / name
            if target.exists():
                existing.append(name)
                continue
            target.write_text(self._initial_content(name), encoding="utf-8")
            created.append(name)
        memory_dir = self.root / "memory"
        memory_dir_created = not memory_dir.exists()
        memory_dir.mkdir(parents=True, exist_ok=True)
        return WorkspaceBootstrapResult(
            root=self.root,
            created_files=created,
            existing_files=existing,
            memory_dir_created=memory_dir_created,
        )

    def stable_context(self) -> str:
        """Compatibility shim for older callers; production boot uses startup_context()."""
        context, _ = self.startup_context(stable_context_used=True)
        return context

    def startup_context(
        self,
        *,
        config: Any | None = None,
        memory: Any | None = None,
        task_ledger: Any | None = None,
        channel: str = "unknown",
        now: datetime | None = None,
        stable_context_used: bool = False,
    ) -> tuple[str, StartupContextReport]:
        sections: list[str] = []
        prompt_sections: list[_PromptSectionDraft] = []

        def add_section(
            section: str,
            *,
            block_id: str,
            title: str,
            source: str,
            trust: PromptTrust,
            priority: int,
            budget_chars: int,
            truncated: bool = False,
        ) -> None:
            sections.append(section)
            prompt_sections.append(
                _PromptSectionDraft(
                    block_id=block_id,
                    title=title,
                    source=source,
                    trust=trust,
                    priority=priority,
                    budget_chars=budget_chars,
                    text=section,
                    truncated=truncated,
                )
            )

        report = StartupContextReport(root=str(self.root), channel=channel)
        today = now or datetime.now().astimezone()
        report.workspace_root = str(self.root)
        report.cwd = str(Path.cwd())
        report.pid = os.getpid()
        report.timestamp = today.isoformat()
        report.code_version = _git_code_version(self.root)
        report.git_status_summary = _git_status_summary(self.root)
        report.git_dirty = bool(report.git_status_summary)
        report.stable_context_used = stable_context_used
        startup_metadata_sections = [
            "# Startup Context",
            f"boot_context_version={BOOT_CONTEXT_VERSION}",
            "startup_context_used=true",
            f"stable_context_used={str(stable_context_used).lower()}",
            f"startup_date={today.strftime('%Y-%m-%d')}",
            f"startup_weekday={today.strftime('%A')}",
            f"startup_channel={channel}",
            f"workspace_root={self.root}",
            f"cwd={report.cwd}",
            f"pid={report.pid}",
            f"code_version={report.code_version or 'unknown'}",
            f"git_dirty={str(report.git_dirty).lower()}",
            f"git_status_entries={len(report.git_status_summary)}",
            "boot_protocol_loaded=pending",
            "boot_protocol_version=pending",
            "memoria persistente=required",
            "task_ledger=required",
            "regla: no asumir API/Pro/modelo/canal sin verificar.",
            "regla: separación persona/modelo/runtime; Dr. Strange es la persona, modelo/runtime/CLI/API/daemon son capas tecnicas.",
            "regla: contexto interno != respuesta externa; reportar fuentes/estado sin imprimir contenido privado completo.",
            "regla: Telegram es canal Telegram cuando current_channel=telegram; no describir Telegram como canal CLI salvo evidencia real de canal CLI.",
        ]
        sections.extend(startup_metadata_sections)
        startup_metadata_text = "\n\n".join(startup_metadata_sections)
        prompt_sections.append(
            _PromptSectionDraft(
                block_id="generated.startup_context",
                title="Startup Context",
                source="startup_context",
                trust="generated",
                priority=80,
                budget_chars=4_000,
                text=startup_metadata_text,
            )
        )
        if report.git_status_summary:
            add_section(
                "## Git Worktree\n"
                "git_dirty=true\n"
                "git_status_sample:\n"
                + "\n".join(f"- {item}" for item in report.git_status_summary),
                block_id="generated.git_worktree",
                title="Git Worktree",
                source="git_status",
                trust="generated",
                priority=60,
                budget_chars=3_000,
            )
        for name in self.STABLE_CONTEXT_FILES:
            content, status = self._read_context_source(
                name,
                self.root / name,
                max_chars=_MAX_CONTEXT_CHARS_PER_FILE,
            )
            report.attempted_sources.append(status)
            if status.status == "missing":
                report.missing_files.append(name)
                continue
            if not content:
                continue
            report.loaded_files.append(name)
            if status.truncated:
                report.truncated_files.append(name)
            if name == "BOOT_PROTOCOL.md":
                report.boot_protocol_loaded = True
                report.boot_protocol_version = _extract_boot_protocol_version(content)
            section = f"## {name}\n{content}"
            add_section(
                section,
                block_id=f"stable.{_prompt_block_slug(name)}",
                title=name,
                source=name,
                trust=_stable_prompt_trust(name),
                priority=_stable_prompt_priority(name),
                budget_chars=_MAX_CONTEXT_CHARS_PER_FILE,
                truncated=status.truncated,
            )

        daily_sections = self._daily_memory_sections(report)
        if daily_sections:
            for section in daily_sections:
                source = _section_source(section, default="memory/")
                add_section(
                    section,
                    block_id=f"memory.{_prompt_block_slug(source)}",
                    title=source,
                    source=source,
                    trust="memory",
                    priority=45,
                    budget_chars=_MAX_DAILY_CONTEXT_CHARS_PER_FILE,
                )

        config_section = self._configuration_section(config, report)
        if config_section:
            add_section(
                config_section,
                block_id="workspace.operational_configuration",
                title="Verified Operational Configuration",
                source="operational_config",
                trust="workspace",
                priority=65,
                budget_chars=4_000,
            )

        memory_sections = self._memory_sections(memory, report)
        if memory_sections:
            for section in memory_sections:
                source = _section_source(section, default="sqlite.memory")
                trust: PromptTrust = "session" if "Session State" in source else "memory"
                add_section(
                    section,
                    block_id=f"{trust}.{_prompt_block_slug(source)}",
                    title=source,
                    source=source,
                    trust=trust,
                    priority=40 if trust == "memory" else 35,
                    budget_chars=6_000,
                )

        task_section = self._task_ledger_section(task_ledger, report)
        if task_section:
            add_section(
                task_section,
                block_id="task_ledger.startup_snapshot",
                title="Task Ledger Startup Snapshot",
                source="task_ledger",
                trust="task_ledger",
                priority=50,
                budget_chars=8_000,
            )

        if not report.loaded_files and config is None and memory is None and task_ledger is None:
            report.prompt_manifest = _build_prompt_manifest(
                [],
                context="",
                context_before_total_truncation="",
                context_truncated=False,
                boot_protocol_loaded=report.boot_protocol_loaded,
                boot_protocol_version=report.boot_protocol_version,
            )
            return "", report
        context_before_total_truncation = (
            _CONTEXT_PREFIX + "\n\n".join(sections) if sections else ""
        )
        context = context_before_total_truncation
        context = context.replace(
            "boot_protocol_loaded=pending",
            f"boot_protocol_loaded={str(report.boot_protocol_loaded).lower()}",
        )
        context = context.replace(
            "boot_protocol_version=pending",
            f"boot_protocol_version={report.boot_protocol_version or 'unknown'}",
        )
        if len(context) > _MAX_STARTUP_CONTEXT_CHARS:
            context = context[:_MAX_STARTUP_CONTEXT_CHARS] + _CONTEXT_TRUNCATED_SUFFIX
            report.context_truncated = True
        report.context_chars = len(context)
        report.prompt_manifest = _build_prompt_manifest(
            prompt_sections,
            context=context,
            context_before_total_truncation=context_before_total_truncation,
            context_truncated=report.context_truncated,
            boot_protocol_loaded=report.boot_protocol_loaded,
            boot_protocol_version=report.boot_protocol_version,
        )
        return context, report

    def system_prompt(
        self,
        fallback: str = "You are Dr. Strange, the autonomous personal agent for Hector Pachano.",
    ) -> str:
        context, _ = self.startup_context()
        if not context:
            return fallback
        return context

    def append_memory(self, entry: str) -> bool:
        """Append a dated entry to MEMORY.md so it persists across restarts.

        Returns True on success, False if the workspace MEMORY.md path is not
        writable. The entry is prefixed with today's date in YYYY-MM-DD form.
        """
        from datetime import datetime, timezone
        import os
        import tempfile

        path = self.root / "MEMORY.md"
        try:
            existing = path.read_text(encoding="utf-8") if path.exists() else "# MEMORY.md\n\n"
            today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
            line = f"- {today}: {entry.strip()}\n"
            if line in existing:
                return True
            new_content = existing.rstrip() + "\n" + line
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), prefix=".MEMORY.md.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(new_content)
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            return True
        except OSError:
            return False

    def _initial_content(self, name: str) -> str:
        if name in _DEFAULT_FILES:
            return _DEFAULT_FILES[name].rstrip() + "\n"
        template = self.template_root / name
        if template.exists():
            return template.read_text(encoding="utf-8").rstrip() + "\n"
        if name == "USER.md":
            return "# USER.md\n\nUser profile and preferences belong here.\n"
        if name == "SOUL.md":
            return "# SOUL.md\n\nYou are Dr. Strange, the autonomous personal agent for Hector Pachano.\n"
        if name == "HEARTBEAT.md":
            return "# HEARTBEAT.md\n\n- If nothing needs attention, reply HEARTBEAT_OK.\n"
        return f"# {name}\n"

    def _read_context_source(
        self,
        name: str,
        path: Path,
        *,
        max_chars: int,
    ) -> tuple[str, ContextSourceStatus]:
        status = ContextSourceStatus(name=name, path=str(path), status="missing")
        if not path.exists():
            return "", status
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            status.status = "error"
            status.error = f"{type(exc).__name__}: {exc}"
            return "", status
        status.status = "loaded"
        status.chars = len(content)
        if len(content) > max_chars:
            content = content[:max_chars] + "\n\n[... truncated]"
            status.truncated = True
        return content, status

    def _daily_memory_sections(self, report: StartupContextReport) -> list[str]:
        memory_dir = self.root / "memory"
        status = ContextSourceStatus(
            name="memory/",
            path=str(memory_dir),
            status="missing",
        )
        if not memory_dir.exists():
            report.attempted_sources.append(status)
            report.missing_files.append("memory/")
            return []
        status.status = "loaded"
        report.attempted_sources.append(status)
        files = sorted(memory_dir.glob("20??-??-??.md"), reverse=True)[:5]
        if not files:
            return ["# Daily Working Notes\nNo dated memory files found."]
        report.daily_memory_loaded = True
        sections = [
            "# Daily Working Notes\nDated temporary context. Treat dates as authoritative for temporal references."
        ]
        for path in files:
            content, source_status = self._read_context_source(
                f"memory/{path.name}",
                path,
                max_chars=_MAX_DAILY_CONTEXT_CHARS_PER_FILE,
            )
            report.attempted_sources.append(source_status)
            if source_status.status != "loaded":
                if source_status.status == "missing":
                    report.missing_files.append(f"memory/{path.name}")
                continue
            report.loaded_files.append(f"memory/{path.name}")
            report.daily_memory_files.append(path.name)
            if source_status.truncated:
                report.truncated_files.append(f"memory/{path.name}")
            sections.append(f"## memory/{path.name}\n{content}")
        return sections

    def _configuration_section(self, config: Any | None, report: StartupContextReport) -> str:
        if config is None:
            return ""
        report.configuration_loaded = True
        active_channels: list[str] = []
        if getattr(config, "telegram_bot_token", None) and getattr(
            config, "telegram_allowed_user_id", None
        ):
            active_channels.append("telegram")
        if getattr(config, "web_chat_enabled", False):
            active_channels.append("web_chat")
        active_channels.append("daemon")
        report.active_channels = active_channels

        def lane_line(lane: str) -> str:
            try:
                provider = config.provider_for_lane(lane)
                model = config.model_for_lane(lane)
                effort = config.effort_for_lane(lane)
            except Exception:
                return f"{lane}=unverified"
            return f"{lane}={provider}:{model} effort={effort}"

        runtime_config_path = getattr(config, "runtime_config_path", None)
        lines = [
            "# Verified Operational Configuration",
            "configuration_operational=verified_at_startup",
            f"active_channels={', '.join(active_channels)}",
            f"workspace_root={getattr(config, 'workspace_root', '')}",
            f"db_path={getattr(config, 'db_path', '')}",
            f"telemetry_root={getattr(config, 'telemetry_root', '')}",
            f"agent_state_root={getattr(config, 'agent_state_root', '')}",
            f"runtime_config_path={runtime_config_path if runtime_config_path else 'built-in defaults'}",
            f"web_chat={getattr(config, 'web_chat_host', 'unknown')}:{getattr(config, 'web_chat_port', 'unknown')}",
            f"browse_backend={getattr(config, 'browse_backend', 'unknown')}",
            f"chrome_cdp_enabled={getattr(config, 'chrome_cdp_enabled', 'unknown')}",
            f"computer_use_enabled={getattr(config, 'computer_use_enabled', 'unknown')}",
            f"claude_auth_mode={getattr(config, 'claude_auth_mode', 'unknown')}",
            lane_line("brain"),
            lane_line("worker"),
            lane_line("research"),
            lane_line("verifier"),
            lane_line("judge"),
            "privacy=do not print or repeat API keys, tokens, cookies, passwords, or credentials.",
            "configuration_rule=no asumir API/Pro/modelo/canal/rutas/permisos; inspect local evidence before answering technical status.",
            "layer_rule=separación persona/modelo/runtime: Dr. Strange is the persona; provider/model/API/CLI/daemon are implementation details.",
        ]
        return "\n".join(lines)

    def _memory_sections(self, memory: Any | None, report: StartupContextReport) -> list[str]:
        if memory is None:
            return []
        sections: list[str] = []
        retention_counts = {
            "always_in_prompt": 0,
            "retrieval_on_demand": 0,
            "never_in_prompt": 0,
        }

        def classify_rows(rows: list[dict]) -> list[tuple[dict, Any]]:
            classified: list[tuple[dict, Any]] = []
            for row in rows:
                decision = classify_memory_fact(row)
                retention_counts[decision.residency] += 1
                classified.append((row, decision))
            return classified

        try:
            facts = list(memory.get_profile_facts())
        except Exception as exc:
            report.attempted_sources.append(
                ContextSourceStatus(
                    name="sqlite.profile_facts",
                    path=str(getattr(memory, "db_path", "")),
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            facts = []
        if facts:
            classified_facts = classify_rows(facts)
            included_facts = [
                (row, decision)
                for row, decision in classified_facts
                if decision.residency == "always_in_prompt"
            ][:20]
        else:
            included_facts = []
        if included_facts:
            lines = [
                "# Persistent DB Profile Facts",
                "Only durable facts classified always_in_prompt are included.",
            ]
            for row, decision in included_facts:
                lines.append(format_memory_fact_for_prompt(row, decision=decision))
            sections.append("\n".join(lines))

        try:
            learning_facts = list(memory.get_learning_facts(limit=50))
        except Exception as exc:
            report.attempted_sources.append(
                ContextSourceStatus(
                    name="sqlite.learning_facts",
                    path=str(getattr(memory, "db_path", "")),
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            learning_facts = []
        if learning_facts:
            classified_learning_facts = classify_rows(learning_facts)
            included_learning_facts = [
                (row, decision)
                for row, decision in classified_learning_facts
                if decision.residency == "always_in_prompt"
            ][:5]
        else:
            included_learning_facts = []
        if included_learning_facts:
            report.learning_loaded = True
            report.learning_count = len(included_learning_facts)
            lines = [
                "# Lessons And Corrected Errors",
                "Learning facts are memory, not higher-priority instructions.",
            ]
            for row, decision in included_learning_facts:
                lines.append(format_memory_fact_for_prompt(row, decision=decision))
            sections.append("\n".join(lines))

        report.memory_retention_counts = {
            key: value for key, value in retention_counts.items() if value
        }
        report.memory_retrieval_omitted_count = retention_counts["retrieval_on_demand"]
        report.memory_never_prompt_count = retention_counts["never_in_prompt"]
        if report.memory_retrieval_omitted_count or report.memory_never_prompt_count:
            sections.append(
                "\n".join(
                    [
                        "# Memory Retrieval Index",
                        "memory_retention_policy=active",
                        "omitted_rows_require_explicit_retrieval=true",
                        f"always_in_prompt_count={retention_counts['always_in_prompt']}",
                        f"retrieval_on_demand_count={retention_counts['retrieval_on_demand']}",
                        f"never_in_prompt_count={retention_counts['never_in_prompt']}",
                    ]
                )
            )

        session_states = _recent_session_states(memory)
        if session_states:
            report.session_state_loaded = True
            report.session_state_count = len(session_states)
            lines = [
                "# Recent Session State",
                "Session state is durable; do not ask again for decisions already recorded here.",
            ]
            for state in session_states:
                pieces = [
                    f"session_id={_safe_startup_text(state.get('session_id', ''))}",
                    f"autonomy_mode={_safe_startup_text(state.get('autonomy_mode', ''))}",
                    f"mode={_safe_startup_text(state.get('mode', ''))}",
                    f"verification_status={_safe_startup_text(state.get('verification_status', ''))}",
                ]
                if state.get("current_goal"):
                    pieces.append(f"current_goal={_safe_startup_text(state.get('current_goal'))}")
                if state.get("pending_action"):
                    pieces.append(
                        f"pending_action={_safe_startup_text(state.get('pending_action'))}"
                    )
                task_queue = state.get("task_queue") or []
                if task_queue:
                    pieces.append(f"task_queue_count={len(task_queue)}")
                active_keys = state.get("active_object_keys") or []
                if active_keys:
                    pieces.append(
                        "active_object_keys="
                        + ",".join(_safe_startup_text(k, limit=80) for k in active_keys[:12])
                    )
                lines.append("- " + " | ".join(pieces))
            sections.append("\n".join(lines))
        return sections

    def _task_ledger_section(self, task_ledger: Any | None, report: StartupContextReport) -> str:
        if task_ledger is None:
            return "# Task Ledger Startup Snapshot\ntask_ledger=unavailable"
        try:
            summary = dict(task_ledger.summary())
            open_tasks = list(task_ledger.list(statuses=("queued", "running"), limit=8))
            recent = list(task_ledger.list(limit=20))
        except Exception as exc:
            return (
                "# Task Ledger Startup Snapshot\n"
                "task_ledger=error\n"
                f"error={type(exc).__name__}: {_safe_startup_text(str(exc))}"
            )
        report.task_ledger_loaded = True
        report.task_ledger_counts = {str(k): int(v) for k, v in summary.items()}
        report.task_ledger_open_count = len(open_tasks)
        attention = [
            task
            for task in recent
            if getattr(task, "status", "") in {"failed", "timed_out", "lost"}
            and getattr(task, "verification_status", "") not in {"passed", "cancelled"}
        ][:5]
        report.task_ledger_attention_count = len(attention)
        lines = [
            "# Task Ledger Startup Snapshot",
            "task_ledger=loaded",
            "Use task_ledger before chat history when reporting task status.",
            f"summary={json.dumps(report.task_ledger_counts, sort_keys=True)}",
        ]
        if open_tasks:
            lines.append("open_tasks:")
            for task in open_tasks:
                lines.append(
                    "- "
                    f"task_id={_safe_startup_text(getattr(task, 'task_id', ''))} "
                    f"session_id={_safe_startup_text(getattr(task, 'session_id', ''))} "
                    f"status={_safe_startup_text(getattr(task, 'status', ''))} "
                    f"verification={_safe_startup_text(getattr(task, 'verification_status', ''))} "
                    f"objective={_safe_startup_text(getattr(task, 'objective', ''))}"
                )
        else:
            lines.append("open_tasks=none")
        if attention:
            lines.append("recent_tasks_needing_attention:")
            for task in attention:
                lines.append(
                    "- "
                    f"task_id={_safe_startup_text(getattr(task, 'task_id', ''))} "
                    f"status={_safe_startup_text(getattr(task, 'status', ''))} "
                    f"verification={_safe_startup_text(getattr(task, 'verification_status', ''))} "
                    f"objective={_safe_startup_text(getattr(task, 'objective', ''))}"
                )
        return "\n".join(lines)


def _safe_startup_text(value: Any, *, limit: int = _MAX_STARTUP_FIELD_CHARS) -> str:
    text = str(redact_sensitive(value, limit=limit))
    text = " ".join(text.split())
    if len(text) > limit:
        return text[:limit].rstrip() + "...[truncated]"
    return text


def _build_prompt_manifest(
    drafts: list[_PromptSectionDraft],
    *,
    context: str,
    context_before_total_truncation: str,
    context_truncated: bool,
    boot_protocol_loaded: bool,
    boot_protocol_version: str,
) -> PromptManifest:
    replacements = {
        "boot_protocol_loaded=pending": f"boot_protocol_loaded={str(boot_protocol_loaded).lower()}",
        "boot_protocol_version=pending": f"boot_protocol_version={boot_protocol_version or 'unknown'}",
    }
    source_texts = [
        _apply_prompt_manifest_replacements(draft.text, replacements) for draft in drafts
    ]
    untruncated_context = _apply_prompt_manifest_replacements(
        context_before_total_truncation, replacements
    )
    included_source_limit = (
        min(len(untruncated_context), _MAX_STARTUP_CONTEXT_CHARS)
        if context_truncated
        else len(untruncated_context)
    )

    blocks: list[PromptBlock] = []
    cursor = len(_CONTEXT_PREFIX) if source_texts else 0
    for index, (draft, source_text) in enumerate(zip(drafts, source_texts)):
        if index > 0:
            cursor += len("\n\n")
        start = cursor
        end = start + len(source_text)
        if start >= included_source_limit:
            included_text = ""
        else:
            included_text = source_text[: max(0, min(end, included_source_limit) - start)]
        blocks.append(
            make_prompt_block(
                block_id=draft.block_id,
                title=draft.title,
                source=draft.source,
                trust=draft.trust,
                priority=draft.priority,
                budget_chars=draft.budget_chars,
                source_text=source_text,
                included_text=included_text,
                source_truncated=draft.truncated,
            )
        )
        cursor = end

    redacted_untruncated_context = str(redact_sensitive(untruncated_context, limit=0))
    redacted_context = str(redact_sensitive(context, limit=0))
    return PromptManifest(
        mode=prompt_capsule_mode_from_env(),
        total_budget_chars=_MAX_STARTUP_CONTEXT_CHARS,
        total_actual_chars=len(redacted_untruncated_context),
        total_included_chars=len(redacted_context),
        blocks=blocks,
    )


def _apply_prompt_manifest_replacements(text: str, replacements: dict[str, str]) -> str:
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def _stable_prompt_trust(name: str) -> PromptTrust:
    if name == "BOOT_PROTOCOL.md":
        return "system"
    if name == "USER.md":
        return "user_profile"
    if name == "MEMORY.md":
        return "memory"
    return "workspace"


def _stable_prompt_priority(name: str) -> int:
    high = {"BOOT_PROTOCOL.md", "IDENTITY.md", "SOUL.md", "USER.md", "AGENTS.md"}
    medium = {"TOOLS.md", "BOOT.md", "HEARTBEAT.md"}
    if name in high:
        return 100
    if name in medium:
        return 65
    if name == "MEMORY.md":
        return 45
    return 50


def _section_source(section: str, *, default: str) -> str:
    first = section.splitlines()[0].strip() if section else ""
    if first.startswith("## "):
        return first[3:].strip() or default
    if first.startswith("# "):
        return first[2:].strip() or default
    return default


def _prompt_block_slug(value: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in value)
    slug = "_".join(part for part in slug.split("_") if part)
    return slug or "block"


def _extract_boot_protocol_version(content: str) -> str:
    for line in content.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower().replace(" ", "_") == "boot_protocol_version":
            return _safe_startup_text(value.strip(), limit=80)
    return "unknown"


def _git_code_version(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return "unknown"
    if completed.returncode != 0:
        return "unknown"
    return _safe_startup_text(completed.stdout.strip(), limit=80) or "unknown"


def _git_status_summary(root: Path, *, limit: int = 20) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "status", "--short"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return []
    if completed.returncode != 0:
        return []
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return [_safe_startup_text(line, limit=160) for line in lines[:limit]]


def _recent_session_states(memory: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    method = getattr(memory, "list_session_states", None)
    if callable(method):
        try:
            return list(method(limit=limit))
        except Exception:
            return []
    conn = getattr(memory, "_conn", None)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT session_id, autonomy_mode, mode, current_goal, pending_action,
                   verification_status, active_object_json, task_queue_json, updated_at
            FROM session_state
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except Exception:
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        active_object = _loads_json_object(row["active_object_json"], default={})
        task_queue = _loads_json_object(row["task_queue_json"], default=[])
        result.append(
            {
                "session_id": row["session_id"],
                "autonomy_mode": row["autonomy_mode"],
                "mode": row["mode"],
                "current_goal": row["current_goal"],
                "pending_action": row["pending_action"],
                "verification_status": row["verification_status"],
                "task_queue": task_queue if isinstance(task_queue, list) else [],
                "active_object_keys": sorted(active_object.keys())
                if isinstance(active_object, dict)
                else [],
                "updated_at": row["updated_at"],
            }
        )
    return result


def _loads_json_object(raw: str | None, *, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default
