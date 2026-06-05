from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Literal

from claw_v2.redaction import redact_sensitive

PromptTrust = Literal[
    "system",
    "workspace",
    "user_profile",
    "memory",
    "session",
    "task_ledger",
    "generated",
]
PromptManifestMode = Literal["shadow", "enforce"]


@dataclass(slots=True)
class PromptBlock:
    block_id: str
    title: str
    source: str
    trust: PromptTrust
    priority: int
    budget_chars: int
    actual_chars: int
    included_chars: int
    sha256: str
    truncated: bool
    redacted: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "title": self.title,
            "source": self.source,
            "trust": self.trust,
            "priority": self.priority,
            "budget_chars": self.budget_chars,
            "actual_chars": self.actual_chars,
            "included_chars": self.included_chars,
            "sha256": self.sha256,
            "truncated": self.truncated,
            "redacted": self.redacted,
        }


@dataclass(slots=True)
class PromptManifest:
    mode: PromptManifestMode
    total_budget_chars: int
    total_actual_chars: int
    total_included_chars: int
    blocks: list[PromptBlock] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "total_budget_chars": self.total_budget_chars,
            "total_actual_chars": self.total_actual_chars,
            "total_included_chars": self.total_included_chars,
            "blocks": [block.to_dict() for block in self.blocks],
        }

    def shadow_diff_payload(self, *, context_truncated: bool) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "context_chars_redacted": self.total_included_chars,
            "context_truncated": context_truncated,
            "total_budget_chars": self.total_budget_chars,
            "total_actual_chars": self.total_actual_chars,
            "total_included_chars": self.total_included_chars,
            "block_count": len(self.blocks),
            "redacted_block_count": sum(1 for block in self.blocks if block.redacted),
            "truncated_block_count": sum(1 for block in self.blocks if block.truncated),
            "shadow_context_unchanged": True,
        }


def prompt_capsule_mode_from_env() -> PromptManifestMode:
    value = os.getenv("CLAW_PROMPT_CAPSULE_MODE", "shadow").strip().lower()
    return "enforce" if value == "enforce" else "shadow"


def make_prompt_block(
    *,
    block_id: str,
    title: str,
    source: str,
    trust: PromptTrust,
    priority: int,
    budget_chars: int,
    source_text: str,
    included_text: str,
    source_truncated: bool = False,
) -> PromptBlock:
    redacted_source = str(redact_sensitive(source_text, limit=0))
    redacted_included = str(redact_sensitive(included_text, limit=0))
    return PromptBlock(
        block_id=block_id,
        title=title,
        source=source,
        trust=trust,
        priority=priority,
        budget_chars=budget_chars,
        actual_chars=len(redacted_source),
        included_chars=len(redacted_included),
        sha256=hashlib.sha256(redacted_included.encode("utf-8")).hexdigest(),
        truncated=source_truncated or len(included_text) < len(source_text),
        redacted=redacted_source != source_text or redacted_included != included_text,
    )
