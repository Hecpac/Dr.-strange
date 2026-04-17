"""Playbook Loader — auto-discovers SKILL.md playbooks and injects them
on-demand into the brain prompt when context matches.

Playbooks are declarative markdown instructions stored in claw_v2/playbooks/.
Each has YAML frontmatter with triggers (keywords that activate the playbook).
Only matched playbooks are loaded, keeping the system prompt lean.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_PLAYBOOKS_DIR = Path(__file__).parent / "playbooks"
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(slots=True)
class Playbook:
    name: str
    triggers: list[str]
    content: str
    priority: int = 0


@dataclass(slots=True)
class PlaybookLoader:
    playbooks: list[Playbook] = field(default_factory=list)
    _loaded: bool = False

    def load(self, directory: Path | None = None) -> None:
        directory = directory or _PLAYBOOKS_DIR
        if not directory.is_dir():
            return
        self.playbooks.clear()
        for path in sorted(directory.glob("*.md")):
            pb = _parse_playbook(path)
            if pb:
                self.playbooks.append(pb)
        self._loaded = True
        logger.debug("Loaded %d playbooks from %s", len(self.playbooks), directory)

    def match(self, message: str, *, max_results: int = 2) -> list[Playbook]:
        if not self._loaded:
            self.load()
        normalized = message.lower()
        scored: list[tuple[int, int, Playbook]] = []
        for pb in self.playbooks:
            hits = sum(1 for t in pb.triggers if t.lower() in normalized)
            if hits > 0:
                scored.append((hits, pb.priority, pb))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [pb for _, _, pb in scored[:max_results]]

    def context_for(self, message: str, *, max_results: int = 2) -> str:
        matched = self.match(message, max_results=max_results)
        if not matched:
            return ""
        sections = []
        for pb in matched:
            sections.append(f"## Playbook: {pb.name}\n{pb.content}")
        return "<playbook-context>\n" + "\n---\n".join(sections) + "\n</playbook-context>"


def _parse_playbook(path: Path) -> Playbook | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm_match = _FRONTMATTER_RE.match(text)
    if not fm_match:
        return None
    frontmatter = fm_match.group(1)
    body = text[fm_match.end():]
    name = path.stem
    triggers: list[str] = []
    priority = 0
    for line in frontmatter.splitlines():
        line = line.strip()
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip('"').strip("'")
        elif line.startswith("- "):
            triggers.append(line[2:].strip().strip('"').strip("'"))
        elif line.startswith("priority:"):
            try:
                priority = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    if not triggers:
        return None
    return Playbook(name=name, triggers=triggers, content=body.strip(), priority=priority)
