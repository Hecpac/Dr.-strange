"""Anthropic API-key resolution for the Claude SDK executor.

Split out of anthropic.py (D1, 2026-06-12). D4 / hallazgo DA.4 (2026-06-12):
resolution is env-only — the process environment or ``~/.claw/env`` (the
daemon environment file sourced by the launcher). Shell dotfiles (.zshrc,
.zprofile, .zshenv, .profile) are never scanned: an interactive-shell secret
must be promoted to the daemon environment explicitly.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from claw_v2.config import AppConfig

_ENV_KEY_PATTERN = re.compile(r"^\s*(?:export\s+)?ANTHROPIC_API_KEY=(?P<value>.+?)\s*$")


def resolve_anthropic_api_key(env_file: Path | None = None) -> str | None:
    """Return the API key from the environment or ``~/.claw/env``, else None."""
    if value := os.getenv("ANTHROPIC_API_KEY"):
        return value.strip() or None
    path = env_file if env_file is not None else Path.home() / ".claw" / "env"
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        match = _ENV_KEY_PATTERN.match(line)
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
