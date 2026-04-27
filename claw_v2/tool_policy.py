from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path


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
)


_DEFAULT_POLICY = ToolPolicy(
    name="<default>",
    risk_level="medium",
    read_only=False,
    allowed_contexts=frozenset({"telegram", "operator"}),
    requires_human=False,
)


TOOL_POLICIES: dict[str, ToolPolicy] = {
    # --- Read-only, low-risk, daemon-safe ---
    "memory.read": ToolPolicy(
        name="memory.read",
        risk_level="low",
        read_only=True,
        allowed_contexts=frozenset({"telegram", "daemon", "brain", "research", "operator"}),
    ),
    "wiki.search": ToolPolicy(
        name="wiki.search",
        risk_level="low",
        read_only=True,
        allowed_contexts=frozenset({"telegram", "daemon", "brain", "research", "operator"}),
    ),
    "task_ledger.read": ToolPolicy(
        name="task_ledger.read",
        risk_level="low",
        read_only=True,
        allowed_contexts=frozenset({"telegram", "daemon", "brain", "operator"}),
    ),
    "git.status": ToolPolicy(
        name="git.status",
        risk_level="low",
        read_only=True,
        allowed_contexts=frozenset({"telegram", "daemon", "operator"}),
    ),
    "observe.recent_events_redacted": ToolPolicy(
        name="observe.recent_events_redacted",
        risk_level="low",
        read_only=True,
        allowed_contexts=frozenset({"telegram", "daemon", "brain", "operator"}),
    ),
    "file.read_workspace_nonsecret": ToolPolicy(
        name="file.read_workspace_nonsecret",
        risk_level="low",
        read_only=True,
        allowed_contexts=frozenset({"telegram", "daemon", "operator"}),
        allowed_paths=("WORKSPACE_ROOT",),
        blocked_path_patterns=SECRET_PATH_PATTERNS,
    ),

    # --- Read-only but NOT daemon-safe (may expose secrets/history) ---
    "Read": ToolPolicy(
        name="Read",
        risk_level="medium",
        read_only=True,
        allowed_contexts=frozenset({"telegram", "operator", "researcher"}),
        blocked_path_patterns=SECRET_PATH_PATTERNS,
    ),
    "file.read": ToolPolicy(
        name="file.read",
        risk_level="medium",
        read_only=True,
        allowed_contexts=frozenset({"telegram", "operator"}),
        blocked_path_patterns=SECRET_PATH_PATTERNS,
    ),
    "observe.recent_events": ToolPolicy(
        name="observe.recent_events",
        risk_level="medium",
        read_only=True,
        allowed_contexts=frozenset({"telegram", "operator"}),
    ),

    # --- Workspace mutations ---
    "Write": ToolPolicy(
        name="Write",
        risk_level="medium",
        read_only=False,
        allowed_contexts=frozenset({"telegram", "operator"}),
        allowed_paths=("WORKSPACE_ROOT",),
    ),
    "Edit": ToolPolicy(
        name="Edit",
        risk_level="medium",
        read_only=False,
        allowed_contexts=frozenset({"telegram", "operator"}),
        allowed_paths=("WORKSPACE_ROOT",),
    ),
    "file.write": ToolPolicy(
        name="file.write",
        risk_level="medium",
        read_only=False,
        allowed_contexts=frozenset({"telegram", "operator"}),
        allowed_paths=("WORKSPACE_ROOT",),
    ),
    "Bash": ToolPolicy(
        name="Bash",
        risk_level="high",
        read_only=False,
        allowed_contexts=frozenset({"telegram", "operator"}),
    ),

    # --- Tier 3 / critical ---
    "social.publish": ToolPolicy(
        name="social.publish",
        risk_level="critical",
        read_only=False,
        allowed_contexts=frozenset({"telegram"}),
        requires_human=True,
    ),
    "pipeline.merge": ToolPolicy(
        name="pipeline.merge",
        risk_level="high",
        read_only=False,
        allowed_contexts=frozenset({"telegram"}),
        requires_human=True,
    ),
    "deploy.production": ToolPolicy(
        name="deploy.production",
        risk_level="critical",
        read_only=False,
        allowed_contexts=frozenset({"telegram"}),
        requires_human=True,
    ),
    "file.delete": ToolPolicy(
        name="file.delete",
        risk_level="high",
        read_only=False,
        allowed_contexts=frozenset({"telegram", "operator"}),
        requires_human=True,
    ),
    "git.force_push": ToolPolicy(
        name="git.force_push",
        risk_level="critical",
        read_only=False,
        allowed_contexts=frozenset({"telegram"}),
        requires_human=True,
    ),
    "WikiDelete": ToolPolicy(
        name="WikiDelete",
        risk_level="high",
        read_only=False,
        allowed_contexts=frozenset({"telegram", "operator"}),
        requires_human=True,
    ),
    "A2ASend": ToolPolicy(
        name="A2ASend",
        risk_level="high",
        read_only=False,
        allowed_contexts=frozenset({"telegram", "operator"}),
        requires_human=True,
    ),
    "HeyGenVideo": ToolPolicy(
        name="HeyGenVideo",
        risk_level="medium",
        read_only=False,
        allowed_contexts=frozenset({"telegram", "operator"}),
        requires_human=True,
    ),
    "GPTImage": ToolPolicy(
        name="GPTImage",
        risk_level="medium",
        read_only=False,
        allowed_contexts=frozenset({"telegram", "operator"}),
        requires_human=True,
    ),
    "SkillExecute": ToolPolicy(
        name="SkillExecute",
        risk_level="high",
        read_only=False,
        allowed_contexts=frozenset({"telegram", "operator"}),
        requires_human=True,
    ),
}


DAEMON_AUTO_APPROVE: frozenset[str] = frozenset({
    "memory.read",
    "wiki.search",
    "task_ledger.read",
    "git.status",
    "observe.recent_events_redacted",
    "file.read_workspace_nonsecret",
})


def policy_for(name: str) -> ToolPolicy:
    return TOOL_POLICIES.get(name, _DEFAULT_POLICY)


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
    base = Path(text).name
    for pattern in SECRET_PATH_PATTERNS:
        if fnmatch.fnmatch(text, pattern):
            return True
        if fnmatch.fnmatch(base, pattern):
            return True
    return False


def validate_workspace_path(path: str | Path, *, workspace_root: str | Path) -> Path:
    candidate = Path(path)
    root = Path(workspace_root).resolve()
    resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"path outside WORKSPACE_ROOT: {path}") from exc
    if path_is_secret(resolved):
        raise PermissionError(f"refusing to access secret path: {path}")
    return resolved
