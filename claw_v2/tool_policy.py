from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote


RISK_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def risk_at_least(value: str, floor: str) -> bool:
    return RISK_ORDER.get(value, 1) >= RISK_ORDER.get(floor, 1)


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    name: str
    risk_level: str
    read_only: bool
    allowed_contexts: frozenset[str]
    requires_human: bool = False
    allowed_paths: tuple[str, ...] = ()
    blocked_path_patterns: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    # Ordered list of tool names to try if this tool is blocked by sandbox
    # or denylist (PermissionError). Used by ToolRegistry.execute_with_pivot.
    # Same args are passed; if they don't fit the alternative the chain
    # continues. Empty by default — no pivoting unless declared.
    fallback_tools: tuple[str, ...] = ()


SECRET_PATH_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*/.env",
    "*/.env.*",
    "*.pem",
    "*.key",
    "*secret*",
    "*credential*",
    "cookies",
    "*/cookies",
    "browser_profile*",
    "*/browser_profile*",
    "approvals/*",
    "*/approvals/*",
    "id_rsa*",
    "*.token",
    # --- 2026-05-29 audit (PR 1): credential files exposed by the HOME read-root ---
    ".netrc",
    "*/.netrc",
    ".npmrc",
    "*/.npmrc",
    "*cookies*",
    "*.keychain",
    "*.keychain-db",
    ".docker/*",
    "*/.docker/*",
    "gh/hosts.yml",
    "*/gh/hosts.yml",
    ".aws/config",  # .aws/credentials already covered by *credential*
    "*/.aws/config",
    ".kube/config",
    "*/.kube/config",
    ".config/gcloud/*",
    "*/.config/gcloud/*",
    "*_history",  # .zsh_history / .bash_history / .python_history (narrower than *history to avoid doc false positives)
    "*.kdbx",
    # SSH private keys: id_rsa* already present; cover the rest + the whole dir
    "id_ed25519*",
    "id_ecdsa*",
    "id_dsa*",
    ".ssh/*",
    "*/.ssh/*",
    # --- 2026-05-31 audit (H3): browser credential stores (named files, not
    # caught by *cookies*). On-disk names are capitalized; matched via the
    # case-insensitive comparison in path_is_secret. ---
    "login data",  # Chrome saved passwords (SQLite, no extension)
    "*/login data",
    "web data",  # Chrome autofill / cards
    "*/web data",
    "key4.db",  # Firefox key store (current)
    "*/key4.db",
    "key3.db",  # Firefox key store (legacy)
    "*/key3.db",
    "logins.json",  # Firefox saved logins
    "*/logins.json",
    "signons.sqlite",  # Firefox saved logins (legacy)
    "*/signons.sqlite",
)


_DEFAULT_POLICY = ToolPolicy(
    name="<default>",
    risk_level="medium",
    read_only=False,
    allowed_contexts=frozenset({"telegram", "operator"}),
    requires_human=False,
)


_VALID_RISK_LEVELS: frozenset[str] = frozenset({"low", "medium", "high", "critical"})

_REQUIRED_FIELDS: tuple[str, ...] = ("risk_level", "read_only", "allowed_contexts")


def _config_path() -> Path:
    return Path(__file__).parent / "config" / "tool_policies.json"


def _expand_pattern_sentinel(value: object) -> tuple[str, ...]:
    """Sentinel "SECRET_PATH_PATTERNS" expands to the in-code tuple. Lists
    pass through. Anything else is a config error.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        if value == "SECRET_PATH_PATTERNS":
            return SECRET_PATH_PATTERNS
        raise ValueError(f"unknown pattern sentinel: {value!r}")
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    raise ValueError(f"blocked_path_patterns must be list or sentinel, got {type(value).__name__}")


def _build_policy(name: str, raw: dict) -> ToolPolicy:
    for field_name in _REQUIRED_FIELDS:
        if field_name not in raw:
            raise ValueError(f"tool {name!r}: missing required field {field_name!r}")
    risk_level = raw["risk_level"]
    if risk_level not in _VALID_RISK_LEVELS:
        raise ValueError(
            f"tool {name!r}: unknown risk_level {risk_level!r} "
            f"(allowed: {sorted(_VALID_RISK_LEVELS)})"
        )
    allowed_contexts = raw["allowed_contexts"]
    if not isinstance(allowed_contexts, list):
        raise ValueError(f"tool {name!r}: allowed_contexts must be a list")
    return ToolPolicy(
        name=name,
        risk_level=risk_level,
        read_only=bool(raw["read_only"]),
        allowed_contexts=frozenset(allowed_contexts),
        requires_human=bool(raw.get("requires_human", False)),
        allowed_paths=tuple(raw.get("allowed_paths", ())),
        blocked_path_patterns=_expand_pattern_sentinel(raw.get("blocked_path_patterns")),
        allowed_domains=tuple(raw.get("allowed_domains", ())),
        fallback_tools=tuple(raw.get("fallback_tools", ())),
    )


def _load_tool_policies_from_config(path: Path) -> dict[str, ToolPolicy]:
    """Load tool policies from a versioned JSON config. Fail-fast on any
    schema or content error — never silently substitute an empty dict, since
    that would let unknown tools fall through to the medium-risk default.
    """
    if not path.exists():
        raise FileNotFoundError(f"tool policies config not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(raw, dict) or "tools" not in raw:
        raise ValueError(f"{path}: top-level object must contain 'tools' key")
    tools_section = raw["tools"]
    if not isinstance(tools_section, dict):
        raise ValueError(f"{path}: 'tools' must be an object mapping name → policy")
    return {name: _build_policy(name, body) for name, body in tools_section.items()}


TOOL_POLICIES: dict[str, ToolPolicy] = _load_tool_policies_from_config(_config_path())


DAEMON_AUTO_APPROVE: frozenset[str] = frozenset({
    "memory.read",
    "wiki.search",
    "task_ledger.read",
    "git.status",
    "observe.recent_events_redacted",
    "file.read_workspace_nonsecret",
})


def policy_for(name: str) -> ToolPolicy:
    policy = TOOL_POLICIES.get(name)
    if policy is None:
        raise KeyError(f"unknown tool policy: {name}")
    return policy


def daemon_can_auto_approve(name: str) -> bool:
    if name not in DAEMON_AUTO_APPROVE:
        return False
    policy = TOOL_POLICIES.get(name)
    if policy is None:
        return False
    return (
        policy.read_only
        and policy.risk_level == "low"
        and "daemon" in policy.allowed_contexts
        and not policy.requires_human
    )


def path_is_secret(candidate: str | Path) -> bool:
    text = str(candidate)
    # SECRET_PATH_PATTERNS are all lowercase; match case-insensitively so macOS
    # browser stores ("Cookies", "Login Data") and capitalized secrets ("ID_RSA")
    # are caught. fnmatch.fnmatch is case-sensitive on POSIX (normcase=identity),
    # so lower both sides and use fnmatchcase for deterministic matching.
    text_lower = text.lower()
    base_lower = Path(text).name.lower()
    for pattern in SECRET_PATH_PATTERNS:
        if fnmatch.fnmatchcase(text_lower, pattern):
            return True
        if fnmatch.fnmatchcase(base_lower, pattern):
            return True
    return False


def _decode_path_text(path: str | Path) -> str:
    # Path objects came from the filesystem and may legitimately contain
    # percent signs in filenames — unquoting them would corrupt the name.
    # Only decode str input, which represents untrusted user-supplied paths
    # where percent-encoding is a known traversal evasion vector.
    if isinstance(path, Path):
        return str(path)
    value = str(path)
    for _ in range(3):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return value


def validate_workspace_path(path: str | Path, *, workspace_root: str | Path, allow_secret: bool = False) -> Path:
    candidate = Path(_decode_path_text(path))
    root = Path(workspace_root).resolve()
    resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"path outside WORKSPACE_ROOT: {path}") from exc
    if not allow_secret and path_is_secret(resolved):
        raise PermissionError(f"refusing to access secret path: {path}")
    return resolved
