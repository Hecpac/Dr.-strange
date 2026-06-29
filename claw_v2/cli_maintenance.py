from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claw_v2.subprocess_runner import run_subprocess_bounded


_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b")
_NPM_FETCH_TIMEOUT_MS = "300000"
_VERSION_TIMEOUT_SECONDS = 20.0
_NPM_VIEW_TIMEOUT_SECONDS = 60.0
_NPM_INSTALL_TIMEOUT_SECONDS = 600.0
_MAX_OUTPUT_CHARS = 4_000


@dataclass(frozen=True, slots=True)
class CliToolSpec:
    name: str
    package: str
    version_command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CliMaintenanceResult:
    verification_status: str
    summary: str
    tool_versions: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    commands_run: tuple[tuple[str, ...], ...] = ()
    installed_packages: tuple[str, ...] = ()
    error: str = ""


CliMaintenanceRunner = Callable[..., subprocess.CompletedProcess[str]]


_TOOLS: tuple[CliToolSpec, ...] = (
    CliToolSpec("codex", "@openai/codex", ("codex", "--version")),
    CliToolSpec("claude", "@anthropic-ai/claude-code", ("claude", "--version")),
)


def run_cli_maintenance_update(
    *,
    cwd: Path | str | None = None,
    runner: CliMaintenanceRunner | None = None,
    observe: Any | None = None,
) -> CliMaintenanceResult:
    commands_run: list[tuple[str, ...]] = []
    tool_versions: dict[str, dict[str, str]] = {}

    def run_command(
        args: Sequence[str],
        *,
        timeout_s: float,
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(str(arg) for arg in args)
        commands_run.append(command)
        if runner is not None:
            return runner(list(command), timeout_s=timeout_s, check=False, cwd=cwd, observe=observe)
        return run_subprocess_bounded(
            command,
            cwd=cwd,
            timeout_s=timeout_s,
            max_output_chars=_MAX_OUTPUT_CHARS,
            check=False,
            observe=observe,
        )

    install_packages: list[str] = []
    for spec in _TOOLS:
        installed_result = _run_version_command(
            run_command, spec.version_command, tool_name=spec.name
        )
        if installed_result[0] is None:
            return _failed_result(
                commands_run,
                tool_versions,
                f"{spec.name} version check failed: {installed_result[1]}",
                installed_packages=install_packages,
            )
        installed_version = installed_result[0]
        latest_result = _run_latest_version_command(run_command, spec.package, tool_name=spec.name)
        if latest_result[0] is None:
            return _failed_result(
                commands_run,
                tool_versions,
                f"{spec.package} latest version check failed: {latest_result[1]}",
                installed_packages=install_packages,
            )
        latest_version = latest_result[0]
        action = "already_current" if installed_version == latest_version else "needs_update"
        tool_versions[spec.name] = {
            "installed": installed_version,
            "latest": latest_version,
            "verified": "",
            "action": action,
        }
        if action == "needs_update":
            install_packages.append(f"{spec.package}@{latest_version}")

    if install_packages:
        install_result = _run_command_capture(
            run_command,
            ["npm", "install", "-g", f"--fetch-timeout={_NPM_FETCH_TIMEOUT_MS}", *install_packages],
            timeout_s=_NPM_INSTALL_TIMEOUT_SECONDS,
        )
        if install_result.returncode != 0:
            error_output = _compact_output(install_result.stderr or install_result.stdout)
            return _failed_result(
                commands_run,
                tool_versions,
                f"npm install failed: {error_output}",
                installed_packages=install_packages,
            )

    for spec in _TOOLS:
        expected = tool_versions[spec.name]["latest"]
        verified_result = _run_version_command(
            run_command, spec.version_command, tool_name=spec.name
        )
        if verified_result[0] is None:
            return _failed_result(
                commands_run,
                tool_versions,
                f"{spec.name} verification failed: {verified_result[1]}",
                installed_packages=install_packages,
            )
        verified_version = verified_result[0]
        tool_versions[spec.name]["verified"] = verified_version
        if verified_version != expected:
            return _failed_result(
                commands_run,
                tool_versions,
                f"{spec.name} verification mismatch: expected {expected}, got {verified_version}",
                installed_packages=install_packages,
            )
        if spec.name in tool_versions and tool_versions[spec.name]["action"] == "needs_update":
            tool_versions[spec.name]["action"] = "updated"

    summary = _success_summary(tool_versions, install_packages)
    return CliMaintenanceResult(
        verification_status="passed",
        summary=summary,
        tool_versions=tool_versions,
        commands_run=tuple(commands_run),
        installed_packages=tuple(install_packages),
    )


def _run_version_command(
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    args: Sequence[str],
    *,
    tool_name: str,
) -> tuple[str | None, str]:
    result = _run_command_capture(runner, args, timeout_s=_VERSION_TIMEOUT_SECONDS)
    if result.returncode != 0:
        return None, _compact_output(
            result.stderr or result.stdout or f"{tool_name} exited non-zero"
        )
    version = _extract_version(result.stdout or result.stderr)
    if version is None:
        return None, _compact_output(result.stdout or result.stderr or "version not found")
    return version, ""


def _run_latest_version_command(
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    package: str,
    *,
    tool_name: str,
) -> tuple[str | None, str]:
    result = _run_command_capture(
        runner,
        ["npm", "view", package, "version"],
        timeout_s=_NPM_VIEW_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return None, _compact_output(
            result.stderr or result.stdout or f"{tool_name} npm view failed"
        )
    version = _extract_version(result.stdout or result.stderr)
    if version is None:
        return None, _compact_output(result.stdout or result.stderr or "latest version not found")
    return version, ""


def _run_command_capture(
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    args: Sequence[str],
    *,
    timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(args, timeout_s=timeout_s)
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(list(args), 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        stdout = exc.output if isinstance(exc.output, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else str(exc)
        return subprocess.CompletedProcess(list(args), 124, stdout, stderr)
    except OSError as exc:
        return subprocess.CompletedProcess(list(args), 1, "", str(exc))


def _extract_version(text: str) -> str | None:
    match = _VERSION_RE.search(text or "")
    return match.group(1) if match else None


def _failed_result(
    commands_run: list[tuple[str, ...]],
    tool_versions: Mapping[str, Mapping[str, str]],
    error: str,
    *,
    installed_packages: Sequence[str],
) -> CliMaintenanceResult:
    return CliMaintenanceResult(
        verification_status="failed",
        summary=f"CLI maintenance failed: {_compact_output(error)}",
        tool_versions=dict(tool_versions),
        commands_run=tuple(commands_run),
        installed_packages=tuple(installed_packages),
        error=_compact_output(error),
    )


def _success_summary(
    tool_versions: Mapping[str, Mapping[str, str]],
    install_packages: Sequence[str],
) -> str:
    details = []
    for tool in _TOOLS:
        versions = tool_versions.get(tool.name, {})
        verified = versions.get("verified") or versions.get("latest") or "unknown"
        action = versions.get("action") or "unknown"
        details.append(f"{tool.name} {verified} ({action})")
    if install_packages:
        return "Updated AI CLIs and verified versions: " + "; ".join(details)
    return "AI CLIs already current and verified: " + "; ".join(details)


def _compact_output(value: str, *, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 15].rstrip() + " ...[truncated]"
