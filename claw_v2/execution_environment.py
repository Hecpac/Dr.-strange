"""Execution environment detector.

Distinguishes whether the current process is:
- ``claude_code_sandbox``: running inside Claude Code's restricted sandbox
  (Bash/Python/browser blocked by the host harness).
- ``claw_production``: the launchd-managed Claw daemon (full permissions).
- ``local_terminal``: developer shell run interactively.
- ``unknown``: nothing matched.

This is the foundation for not letting an environment that cannot execute
real bash/python pretend it can. The capability router and bot use this to
emit ``runtime_handoff`` instead of fake "I'll do it" replies.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Literal


EnvironmentKind = Literal[
    "claude_code_sandbox",
    "claw_production",
    "local_terminal",
    "unknown",
]


_CLAUDE_CODE_ENV_HINTS: tuple[str, ...] = (
    "CLAUDE_CODE_SESSION",
    "CLAUDE_CODE_PROJECT_DIR",
    "CLAUDECODE",
    "ANTHROPIC_CLAUDE_CODE",
)

_CLAW_PRODUCTION_HINTS: tuple[str, ...] = (
    "CLAW_RUNTIME_MODE",
    "CLAW_DAEMON",
    "LAUNCH_DAEMON_LABEL",
)


@dataclass(slots=True)
class ExecutionEnvironment:
    kind: EnvironmentKind
    can_run_bash: bool
    can_run_python_module: bool
    can_access_browser_cli: bool
    can_restart_launchd: bool
    reason: str = ""

    @property
    def is_sandboxed(self) -> bool:
        return self.kind == "claude_code_sandbox"


def _has_claude_code_env() -> bool:
    return any(os.environ.get(name) for name in _CLAUDE_CODE_ENV_HINTS)


def _has_claw_production_env() -> bool:
    if os.environ.get("CLAW_RUNTIME_MODE", "").lower() == "production":
        return True
    return any(os.environ.get(name) for name in _CLAW_PRODUCTION_HINTS)


def _bash_runnable() -> bool:
    return shutil.which("bash") is not None


def _python_module_runnable() -> bool:
    return shutil.which("python3") is not None or shutil.which("python") is not None


def _browser_cli_present(workspace_root: str | None = None) -> bool:
    if workspace_root and os.path.exists(
        os.path.join(workspace_root, "claw_v2", "browser_cli.py")
    ):
        return True
    if shutil.which("browser_cli") is not None:
        return True
    return False


def detect_execution_environment(
    *, workspace_root: str | None = None
) -> ExecutionEnvironment:
    """Detect the active environment using env vars + tool availability.

    Order of checks:
    1. Explicit Claw production marker.
    2. Explicit Claude Code marker.
    3. Fallback to ``local_terminal`` if shell tools available.
    4. ``unknown`` otherwise.
    """
    if _has_claw_production_env():
        return ExecutionEnvironment(
            kind="claw_production",
            can_run_bash=_bash_runnable(),
            can_run_python_module=_python_module_runnable(),
            can_access_browser_cli=_browser_cli_present(workspace_root),
            can_restart_launchd=True,
            reason="explicit_claw_production_env",
        )
    if _has_claude_code_env():
        return ExecutionEnvironment(
            kind="claude_code_sandbox",
            can_run_bash=False,
            can_run_python_module=False,
            can_access_browser_cli=False,
            can_restart_launchd=False,
            reason="claude_code_session_marker_present",
        )
    if _bash_runnable() and _python_module_runnable():
        return ExecutionEnvironment(
            kind="local_terminal",
            can_run_bash=True,
            can_run_python_module=True,
            can_access_browser_cli=_browser_cli_present(workspace_root),
            can_restart_launchd=True,
            reason="local_shell_with_full_tooling",
        )
    return ExecutionEnvironment(
        kind="unknown",
        can_run_bash=False,
        can_run_python_module=False,
        can_access_browser_cli=False,
        can_restart_launchd=False,
        reason="no_recognizable_markers",
    )
