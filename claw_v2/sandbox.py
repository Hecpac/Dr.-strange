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


def check_command(command: str, policy: SandboxPolicy) -> str | None:
    dangerous_fragments = (" rm ", " shutdown", " reboot", "diskutil", " mkfs", " dd ")
    padded = f" {command} "
    if any(fragment in padded for fragment in dangerous_fragments):
        return "dangerous shell fragment detected"
    if policy.network_policy == "none" and any(token in command for token in ("curl ", "wget ", "http://", "https://")):
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
