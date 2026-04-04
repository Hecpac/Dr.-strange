from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

from claw_v2.network_proxy import DomainAllowlistEnforcer, NetworkPolicy
from claw_v2.types import SandboxDecision


@dataclass(slots=True)
class SandboxPolicy:
    workspace_root: Path
    allowed_paths: list[Path] = field(default_factory=list)
    writable_paths: list[Path] = field(default_factory=list)
    network_policy: str = "none"
    credential_scope: str = "workspace"


def is_within_allowed(path: Path, policy: SandboxPolicy) -> bool:
    roots = [policy.workspace_root, *policy.allowed_paths, *policy.writable_paths]
    resolved = path.expanduser().resolve(strict=False)
    return any(resolved.is_relative_to(root.expanduser().resolve(strict=False)) for root in roots)


def _unwrap_command_tokens(tokens: list[str]) -> list[str]:
    remaining = list(tokens)
    while remaining:
        base_cmd = Path(remaining[0]).name
        if base_cmd in {"bash", "sh", "zsh", "fish", "dash"}:
            if "-c" not in remaining:
                return remaining
            c_idx = remaining.index("-c")
            if c_idx + 1 >= len(remaining):
                return remaining
            try:
                return shlex.split(remaining[c_idx + 1])
            except ValueError:
                return []
        if base_cmd in {"env", "command", "sudo", "nice", "nohup"}:
            idx = 1
            if base_cmd == "env":
                while idx < len(remaining) and (
                    "=" in remaining[idx]
                    and not remaining[idx].startswith("-")
                    and remaining[idx].split("=", 1)[0]
                ):
                    idx += 1
            else:
                while idx < len(remaining) and remaining[idx].startswith("-"):
                    idx += 1
                    if base_cmd == "sudo" and idx < len(remaining) and remaining[idx - 1] in {"-u", "-g", "-h", "-p"}:
                        idx += 1
                    if base_cmd == "nice" and idx < len(remaining) and remaining[idx - 1] == "-n":
                        idx += 1
            remaining = remaining[idx:]
            continue
        if base_cmd == "xargs":
            return remaining
        return remaining
    return remaining


def check_command(command: str, policy: SandboxPolicy) -> str | None:
    dangerous_commands = {"rm", "shutdown", "reboot", "diskutil", "mkfs", "dd"}
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "unparseable command"
    if not tokens:
        return None
    tokens = _unwrap_command_tokens(tokens)
    if not tokens:
        return "unparseable command"
    if "xargs" in [Path(token).name for token in tokens]:
        return "xargs is not allowed"
    base_cmd = Path(tokens[0]).name
    if base_cmd in dangerous_commands:
        return "dangerous command detected"
    if policy.network_policy == "none" and base_cmd in {"curl", "wget"}:
        return "network access not allowed for this agent"
    return None


def sandbox_hook(
    tool_name: str,
    tool_input: dict,
    *,
    policy: SandboxPolicy,
    network_enforcer: DomainAllowlistEnforcer | None = None,
    actor: str = "default",
) -> SandboxDecision:
    if tool_name in {"Read", "Write", "Edit", "mcp__claw_tools__write_file"}:
        path = Path(tool_input.get("file_path") or tool_input.get("path") or "")
        allowed = is_within_allowed(path, policy)
        return SandboxDecision(allowed, "" if allowed else "path outside allowed boundaries")

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        violation = check_command(command, policy)
        if violation:
            return SandboxDecision(False, violation)
        tokens = shlex.split(command) if command else []
        for token in tokens:
            if token.startswith("/") and not is_within_allowed(Path(token), policy):
                return SandboxDecision(False, "command references path outside allowed boundaries")
        return SandboxDecision(True)

    if tool_name in {"WebSearch", "WebFetch"} and network_enforcer is not None:
        url = tool_input.get("url") or tool_input.get("target") or ""
        domains = tool_input.get("allowed_domains") or []
        if url:
            network_policy = NetworkPolicy(allowed_domains=domains or ["*"], blocked_domains=[])
            return network_enforcer.enforce_url(url, policy=network_policy, actor=actor)
    return SandboxDecision(True)
