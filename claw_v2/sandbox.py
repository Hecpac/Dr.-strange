from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

from claw_v2.network_proxy import DomainAllowlistEnforcer, NetworkPolicy
from claw_v2.types import SandboxDecision


SURGICAL_ALLOWED_BINARIES = frozenset(
    {
        "cat",
        "cp",
        "curl",
        "git",
        "grep",
        "launchctl",
        "ls",
        "mkdir",
        "mv",
        "open",
        "osascript",
        "pwd",
        "rg",
        "touch",
        "wget",
    }
)

DEVELOPMENT_ALLOWED_BINARIES = frozenset(
    {
        "node",
        "python",
        "python3",
    }
)


CAPABILITY_PROFILES = {
    "surgical": SURGICAL_ALLOWED_BINARIES,
    "engineer": SURGICAL_ALLOWED_BINARIES | DEVELOPMENT_ALLOWED_BINARIES,
    "admin": SURGICAL_ALLOWED_BINARIES | DEVELOPMENT_ALLOWED_BINARIES | {"brew"},
}

DEFAULT_ALLOWED_BINARIES = CAPABILITY_PROFILES["surgical"]

PYTHON_INTERPRETERS = frozenset({"python", "python3"})
PYTHON_SAFE_MODULES = frozenset({"compileall", "claw_v2.browser_cli", "py_compile", "pytest", "unittest", "venv"})
NODE_INTERPRETERS = frozenset({"node"})
VERSION_OR_HELP_FLAGS = frozenset({"--version", "-V", "-VV", "--help", "-h"})
SHELL_CONTROL_TOKENS = frozenset({";", "|", "||", "&", "&&", "`", "<", ">", ">>", "<<", "$("})


@dataclass(slots=True)
class SandboxPolicy:
    workspace_root: Path
    allowed_paths: list[Path] = field(default_factory=list)
    writable_paths: list[Path] = field(default_factory=list)
    network_policy: str = "none"
    credential_scope: str = "workspace"
    capability_profile: str = "surgical"
    allowed_binaries: set[str] | None = None

    @property
    def active_profile_binaries(self) -> set[str]:
        if self.allowed_binaries is not None:
            return set(self.allowed_binaries)
        return set(CAPABILITY_PROFILES.get(self.capability_profile, DEFAULT_ALLOWED_BINARIES))


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
    if "\n" in command or "\r" in command:
        return "shell command separators are not allowed"
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "unparseable command"
    if not tokens:
        return None
    if _tokens_contain_shell_controls(tokens):
        return "shell operators (;|&`<>$) are not allowed"
    tokens = _unwrap_command_tokens(tokens)
    if not tokens:
        return "unparseable command"
    if _tokens_contain_shell_controls(tokens):
        return "shell operators (;|&`<>$) are not allowed"
    if "xargs" in [Path(token).name for token in tokens]:
        return "xargs is not allowed"
    base_cmd = Path(tokens[0]).name
    if _network_disabled(policy) and base_cmd in {"curl", "wget"}:
        return "network access not allowed for this agent"
    if base_cmd not in policy.active_profile_binaries:
        return f"binary '{base_cmd}' requires higher privilege level (not in the allowed whitelist)"
    interpreter_violation = _check_interpreter_invocation(base_cmd, tokens, policy)
    if interpreter_violation:
        return interpreter_violation
    return None


def _tokens_contain_shell_controls(tokens: list[str]) -> bool:
    for token in tokens:
        if token in SHELL_CONTROL_TOKENS:
            return True
        if any(char in token for char in (";", "|", "&", "`", "<", ">")):
            return True
        if token.startswith("$("):
            return True
    return False


def _check_interpreter_invocation(base_cmd: str, tokens: list[str], policy: SandboxPolicy) -> str | None:
    if base_cmd in PYTHON_INTERPRETERS:
        return _check_python_invocation(tokens, policy)
    if base_cmd in NODE_INTERPRETERS:
        return _check_node_invocation(tokens, policy)
    return None


def _check_python_invocation(tokens: list[str], policy: SandboxPolicy) -> str | None:
    args = tokens[1:]
    if not args:
        return "interactive python execution is not allowed; run an explicit workspace script"
    if _contains_only_version_or_help_flags(args):
        return None
    for index, arg in enumerate(args):
        if arg == "-" or arg == "-i":
            return "interactive python execution is not allowed; run an explicit workspace script"
        if arg == "-c" or arg.startswith("-c"):
            return "inline python execution is not allowed; write a workspace script and run it"
        if arg == "-m":
            module_name = args[index + 1] if index + 1 < len(args) else ""
            return _check_python_module(module_name)
        if arg.startswith("-m") and len(arg) > 2:
            return _check_python_module(arg[2:])
    script = _first_non_option_arg(args)
    if script is None:
        return "python execution requires an explicit workspace script"
    if not script.endswith(".py"):
        return "python execution is limited to explicit .py scripts or safe -m modules"
    if not _script_path_within_policy(script, policy):
        return "python script path outside allowed boundaries"
    return None


def _check_python_module(module_name: str) -> str | None:
    normalized = module_name.strip()
    if normalized in PYTHON_SAFE_MODULES:
        return None
    return f"python module '{normalized or '<missing>'}' is not in the safe module allowlist"


def _check_node_invocation(tokens: list[str], policy: SandboxPolicy) -> str | None:
    args = tokens[1:]
    if not args:
        return "interactive node execution is not allowed; run an explicit workspace script"
    if _contains_only_version_or_help_flags(args):
        return None
    for arg in args:
        if arg in {"-", "-i", "-e", "--eval", "-p", "--print"}:
            return "inline node execution is not allowed; write a workspace script and run it"
        if arg.startswith("--eval=") or arg.startswith("--print="):
            return "inline node execution is not allowed; write a workspace script and run it"
    script = _first_non_option_arg(args)
    if script is None:
        return "node execution requires an explicit workspace script"
    if not script.endswith((".js", ".mjs", ".cjs")):
        return "node execution is limited to explicit .js, .mjs, or .cjs scripts"
    if not _script_path_within_policy(script, policy):
        return "node script path outside allowed boundaries"
    return None


def _contains_only_version_or_help_flags(args: list[str]) -> bool:
    return bool(args) and all(arg in VERSION_OR_HELP_FLAGS for arg in args)


def _first_non_option_arg(args: list[str]) -> str | None:
    value_taking_options = {"-W", "-X", "--check-hash-based-pycs", "--encoding"}
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in value_taking_options:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return None


def _script_path_within_policy(script: str, policy: SandboxPolicy) -> bool:
    try:
        resolved = _resolve_path_for_policy(Path(script), policy)
    except OSError:
        return False
    roots = [root.expanduser().resolve(strict=False) for root in _path_roots(policy)]
    return any(resolved.is_relative_to(root) for root in roots)


def _network_disabled(policy: SandboxPolicy) -> bool:
    if isinstance(policy.network_policy, str):
        return policy.network_policy == "none"
    if isinstance(policy.network_policy, NetworkPolicy):
        return False
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
