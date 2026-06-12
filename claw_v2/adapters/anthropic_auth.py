"""Anthropic API-key resolution for the Claude SDK executor.

Split out of anthropic.py (D1, 2026-06-12). Pure move: behavior must match
the helpers it replaces.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from claw_v2.config import AppConfig


def resolve_anthropic_api_key() -> str | None:
    if value := os.getenv("ANTHROPIC_API_KEY"):
        return value.strip() or None
    pattern = re.compile(r"^\s*(?:export\s+)?ANTHROPIC_API_KEY=(?P<value>.+?)\s*$")
    for path in (
        Path.home() / ".zshrc",
        Path.home() / ".zprofile",
        Path.home() / ".zshenv",
        Path.home() / ".profile",
    ):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except FileNotFoundError:
            continue
        for line in reversed(lines):
            match = pattern.match(line)
            if match is None:
                continue
            value = match.group("value").strip().strip("\"'")
            if value:
                return value
    return None


def should_use_api_key_auth(config: AppConfig) -> bool:
    if config.claude_auth_mode == "api_key":
        return True
    if config.claude_auth_mode == "auto":
        return resolve_anthropic_api_key() is not None
    return False
