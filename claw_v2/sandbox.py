from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

from claw_v2.network_proxy import DomainAllowlistEnforcer, NetworkPolicy
from claw_v2.types import SandboxDecision


DEFAULT_ALLOWED_BINARIES = frozenset(
    {
        "cat",
        "cp",
        "curl",
        "git",
        "grep",
        "ls",
        "mkdir",
        "mv",
        "pwd",
        "rg",
        "touch",
        "wget",
    }
)


@dataclass(slots=True)
class SandboxPolicy:
    workspace_root: Path
    allowed_paths: list[Path] = field(default_factory=list)
    writable_paths: list[Path] = field(default_factory=list)
    network_policy: str = "none"
    credential_scope: str = "workspace"
    allowed_binaries: set[str] = field(default_factory=lambda: set(DEFAULT_ALLOWED_BINARIES))


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


_SHELL_OPERATORS_RE = __import__("re").compile(r"[;|&`<>]|\$\(")


def check_command(command: str, policy: SandboxPolicy) -> str | None:
    if _SHELL_OPERATORS_RE.search(command):
        return "shell operators (;|&`<>$) are not allowed"
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
    if _network_disabled(policy) and base_cmd in {"curl", "wget"}:
        return "network access not allowed for this agent"
    if base_cmd not in policy.allowed_binaries:
        return f"binary '{base_cmd}' is not in the allowed whitelist"
    return None


def _network_disabled(policy: SandboxPolicy) -> bool:
    if isinstance(policy.network_policy, str):
        return policy.network_policy == "none"
    if isinstance(policy.network_policy, NetworkPolicy):
        return not policy.network_policy.allowed_domains
    return False


def _path_roots(policy: SandboxPolicy, system_roots: list[Path] | None = None) -> list[Path]:
    return [policy.workspace_root, *policy.allowed_paths, *policy.writable_paths, *(system_roots or [])]


def _resolve_path_for_policy(path: Path, policy: SandboxPolicy) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = policy.workspace_root / expanded
    return expanded.resolve(strict=False)


def _path_candidate_token(token: str) -> str | None:
    if not token or "://" in token:
        return None
    candidate = token
    if token.startswith("-"):
        if "=" not in token:
            return None
        candidate = token.split("=", 1)[1]
    if "=" in candidate and not candidate.startswith(("/", ".", "~")):
        return None
    return candidate


def _is_path_token(token: str, policy: SandboxPolicy) -> bool:
    candidate = _path_candidate_token(token)
    if candidate is None:
        return False
    if "/" in candidate or candidate.startswith((".", "~")) or Path(candidate).is_absolute():
        return True
    return (policy.workspace_root / candidate).exists()


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
        # System directories that are safe to reference (read-only tools, interpreters).
        _SYSTEM_ROOTS = [Path("/usr"), Path("/bin"), Path("/sbin"), Path("/opt"), Path("/tmp"), Path("/private/tmp")]
        tokens = shlex.split(command) if command else []
        for token in tokens:
            if not _is_path_token(token, policy):
                continue
            path_token = _path_candidate_token(token)
            if path_token is None:
                continue
            try:
                normalized = _resolve_path_for_policy(Path(path_token), policy)
            except OSError:
                return SandboxDecision(False, "invalid path resolution")
            roots = [root.expanduser().resolve(strict=False) for root in _path_roots(policy, _SYSTEM_ROOTS)]
            if not any(normalized.is_relative_to(root) for root in roots):
                return SandboxDecision(False, "command references path outside allowed boundaries")
        return SandboxDecision(True)

    if tool_name in {"WebSearch", "WebFetch"} and network_enforcer is not None:
        url = tool_input.get("url") or tool_input.get("target") or ""
        if url:
            return network_enforcer.enforce_url(url, policy=policy.network_policy if hasattr(policy, "network_policy") and isinstance(policy.network_policy, NetworkPolicy) else NetworkPolicy(allowed_domains=[], blocked_domains=[]), actor=actor)
    return SandboxDecision(True)
