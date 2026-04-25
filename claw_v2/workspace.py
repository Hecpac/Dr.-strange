from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


_MAX_CONTEXT_CHARS_PER_FILE = 30_000


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

Name: Claw
Role: Autonomous local agent for Hector Pachano
Primary language: Spanish
""",
    "MEMORY.md": """# MEMORY.md

Durable memories, preferences, and decisions belong here.
Keep entries concise and evidence-backed.
""",
    "BOOT.md": """# BOOT.md

Startup checklist for runtime restarts.

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


class AgentWorkspace:
    """Workspace-first context and memory bootstrap for the agent runtime."""

    STABLE_CONTEXT_FILES = (
        "SOUL.md",
        "AGENTS.md",
        "USER.md",
        "IDENTITY.md",
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
        "BOOT.md",
    )

    def __init__(self, root: Path | str, *, template_root: Path | str | None = None) -> None:
        self.root = Path(root)
        self.template_root = Path(template_root) if template_root is not None else Path(__file__).parent

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
        sections: list[str] = []
        for name in self.STABLE_CONTEXT_FILES:
            path = self.root / name
            if not path.exists():
                continue
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                continue
            if len(content) > _MAX_CONTEXT_CHARS_PER_FILE:
                content = content[:_MAX_CONTEXT_CHARS_PER_FILE] + "\n\n[... truncated]"
            sections.append(f"## {name}\n{content}")
        return "# Agent Workspace Context\n\n" + "\n\n".join(sections) if sections else ""

    def system_prompt(self, fallback: str = "You are Claw.") -> str:
        context = self.stable_context()
        if not context:
            return fallback
        return context

    def _initial_content(self, name: str) -> str:
        if name in _DEFAULT_FILES:
            return _DEFAULT_FILES[name].rstrip() + "\n"
        template = self.template_root / name
        if template.exists():
            return template.read_text(encoding="utf-8").rstrip() + "\n"
        if name == "USER.md":
            return "# USER.md\n\nUser profile and preferences belong here.\n"
        if name == "SOUL.md":
            return "# SOUL.md\n\nYou are Claw, an autonomous local agent.\n"
        if name == "HEARTBEAT.md":
            return "# HEARTBEAT.md\n\n- If nothing needs attention, reply HEARTBEAT_OK.\n"
        return f"# {name}\n"
