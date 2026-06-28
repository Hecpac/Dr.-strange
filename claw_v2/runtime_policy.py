from __future__ import annotations

import os
import shlex
import fnmatch
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse

from claw_v2.network_proxy import DomainAllowlistEnforcer, NetworkPolicy
from claw_v2.sandbox import SandboxPolicy, check_command, _unwrap_command_tokens
from claw_v2.tool_policy import TOOL_POLICIES, ToolPolicy, _decode_path_text, path_is_secret

# Mirrors tools.TIER_REQUIRES_APPROVAL (importing tools here would be circular).
_TIER_REQUIRES_APPROVAL = 3
_PROTECTED_GIT_COMMIT_BRANCHES = frozenset({"main", "master", "prod", "production"})
# git top-level options that consume a SEPARATE-arg value. Any value-taking
# global option missing here makes the parser skip it as valueless and then bail
# to None at its value token, letting a `commit` past the protected-branch guard
# (issue #153 bypass class). Keep in sync with git's option set.
_GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    {
        "-C",
        "-c",
        "--attr-source",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--namespace",
        "--super-prefix",
        "--work-tree",
    }
)
_GIT_GLOBAL_OPTIONS_WITH_ATTACHED_VALUE = (
    "--attr-source=",
    "--config-env=",
    "--exec-path=",
    "--git-dir=",
    "--namespace=",
    "--super-prefix=",
    "--work-tree=",
)
_GIT_BRANCH_CHECK_TIMEOUT_SECONDS = 5

if TYPE_CHECKING:
    from claw_v2.tools import ToolDefinition


class RuntimePolicyViolation(PermissionError):
    """Raised when a runtime action violates the centralized tool policy."""


@dataclass(frozen=True, slots=True)
class RuntimePolicyDecision:
    tool_name: str
    policy: ToolPolicy
    approval_required: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeApprovalDefinition:
    """Small approval-gate adapter for non-ToolRegistry call sites."""

    name: str
    tier: int
    mutates_state: bool = False
    requires_network: bool = False


ApprovalGate = Callable[[Any, dict], None]


_DEFAULT_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "TERM",
        "LANG",
        "LC_ALL",
        "SHELL",
        "USER",
        "TMPDIR",
        "CLAW_RUNTIME_MODE",
        "CLAW_HOME",
    }
)
_DEFAULT_CHILD_ENV_DENY_SUBSTRINGS = frozenset(
    {
        "KEY",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "COOKIE",
        "CREDENTIAL",
        "AUTH",
        "PRIVATE",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeChildEnvPolicy:
    allowlist: frozenset[str] = _DEFAULT_CHILD_ENV_ALLOWLIST
    deny_substrings: frozenset[str] = _DEFAULT_CHILD_ENV_DENY_SUBSTRINGS

    def allows_name(self, name: str) -> bool:
        normalized = str(name).upper()
        if any(fragment in normalized for fragment in self.deny_substrings):
            return False
        return normalized in self.allowlist


@dataclass(frozen=True, slots=True)
class SanitizedChildEnv:
    env: dict[str, str]
    preserved_count: int
    dropped_count: int
    dropped_sensitive_count: int

    def to_metadata(self) -> dict[str, int | str]:
        return {
            "policy": "RuntimeChildEnvPolicy",
            "preserved_count": self.preserved_count,
            "dropped_count": self.dropped_count,
            "dropped_sensitive_count": self.dropped_sensitive_count,
        }


def sanitize_child_env(
    source_env: dict[str, str] | None = None,
    *,
    policy: RuntimeChildEnvPolicy | None = None,
) -> SanitizedChildEnv:
    """Return an allowlisted child-process environment without secret values."""

    selected_policy = policy or RuntimeChildEnvPolicy()
    source = dict(os.environ if source_env is None else source_env)
    clean: dict[str, str] = {}
    dropped_sensitive = 0
    for key, value in source.items():
        normalized = str(key).upper()
        sensitive = any(fragment in normalized for fragment in selected_policy.deny_substrings)
        if sensitive:
            dropped_sensitive += 1
            continue
        if normalized not in selected_policy.allowlist:
            continue
        clean[normalized] = str(value)
    return SanitizedChildEnv(
        env=clean,
        preserved_count=len(clean),
        dropped_count=max(len(source) - len(clean), 0),
        dropped_sensitive_count=dropped_sensitive,
    )


_PATH_KEYS = frozenset(
    {
        "path",
        "file_path",
        "image_path",
        "input_path",
        "output_path",
        "source_path",
        "target_path",
        "destination_path",
        "dest_path",
        "directory",
        "dir",
        "root",
        "cwd",
        "storage_root",
        "media_path",
    }
)
_URL_KEYS = frozenset({"url", "target", "endpoint", "base_url", "callback_url", "image_url"})
_URL_LIST_KEYS = frozenset({"urls", "redirect_chain"})
_SYSTEM_ROOTS = [
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/opt"),
    Path("/tmp"),
    Path("/private/tmp"),
]
_DEFAULT_TOOL_URLS = {
    "HeyGenVideo": ("https://api.heygen.com/",),
    "heygen.video.generate": ("https://api.heygen.com/",),
    "GPTImage": ("https://api.openai.com/",),
    "AnalyzeImage": ("https://api.openai.com/",),
    "FirecrawlScrape": ("https://api.firecrawl.dev/",),
    "FirecrawlSearch": ("https://api.firecrawl.dev/",),
    "FirecrawlExtract": ("https://api.firecrawl.dev/",),
    "notebooklm.list": ("https://notebooklm.google.com/",),
    "notebooklm.status": ("https://notebooklm.google.com/",),
    "notebooklm.chat": ("https://notebooklm.google.com/",),
    "notebooklm.create": ("https://notebooklm.google.com/",),
    "notebooklm.delete": ("https://notebooklm.google.com/",),
    "notebooklm.add_sources": ("https://notebooklm.google.com/",),
    "notebooklm.add_text": ("https://notebooklm.google.com/",),
    "notebooklm.start_research": ("https://notebooklm.google.com/",),
    "notebooklm.start_artifact": ("https://notebooklm.google.com/",),
}


class RuntimePolicyEngine:
    """Single runtime gate for tool, filesystem, shell, approval, and network policy.

    The engine is fail-closed: a tool must exist in ``tool_policies.json`` and
    the current context must be explicitly allowed before any lower-level checks
    run.
    """

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        sandbox_policy: SandboxPolicy | None = None,
        network_enforcer: DomainAllowlistEnforcer | None = None,
        approval_gate: ApprovalGate | None = None,
        autoexec_max_tier: int = 2,
        policies: dict[str, ToolPolicy] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
        self.sandbox_policy = sandbox_policy
        self.network_enforcer = network_enforcer
        self.approval_gate = approval_gate
        # AM-T3FLOOR (2026-06-12): autoexec_max_tier is a CEILING, never an
        # override — Tier 3 always hits the approval gate (core invariant).
        # Clamp so a misconfigured env value (e.g. 3) cannot disable the gate.
        self.autoexec_max_tier = min(int(autoexec_max_tier), _TIER_REQUIRES_APPROVAL - 1)
        self.policies = policies if policies is not None else TOOL_POLICIES

    def enforce_tool(
        self,
        definition: "ToolDefinition",
        args: dict,
        *,
        context: str,
        approval_gate: ApprovalGate | None = None,
    ) -> RuntimePolicyDecision:
        return self.enforce(
            definition.name,
            args,
            context=context,
            tier=definition.tier,
            mutates_state=definition.mutates_state,
            requires_network=definition.requires_network,
            approval_gate=approval_gate,
            approval_definition=definition,
        )

    def enforce(
        self,
        tool_name: str,
        args: dict | None = None,
        *,
        context: str,
        tier: int | None = None,
        mutates_state: bool | None = None,
        requires_network: bool | None = None,
        approval_gate: ApprovalGate | None = None,
        approval_definition: Any | None = None,
    ) -> RuntimePolicyDecision:
        tool_args = dict(args or {})
        policy = self.policies.get(tool_name)
        if policy is None:
            raise RuntimePolicyViolation(
                f"Tool '{tool_name}' is not declared in tool_policies.json"
            )

        contexts = _context_candidates(context)
        if not contexts.intersection(policy.allowed_contexts):
            allowed = ", ".join(sorted(policy.allowed_contexts)) or "<none>"
            raise RuntimePolicyViolation(
                f"Tool '{tool_name}' is not allowed in context '{context}' (allowed: {allowed})"
            )

        action_mutates = bool(mutates_state)
        if policy.read_only and action_mutates:
            raise RuntimePolicyViolation(
                f"Tool '{tool_name}' is read-only by policy but requested mutation"
            )

        if tool_name == "Bash":
            self._enforce_command(str(tool_args.get("command") or ""))
        self._enforce_paths(tool_name, tool_args, policy)
        self._enforce_network(
            tool_name,
            tool_args,
            policy,
            context=context,
            requires_network=bool(requires_network),
        )

        effective_tier = tier if tier is not None else _tier_for_policy(policy)
        # AM-T3FLOOR: the >= floor is unconditional — independent of the
        # (already clamped) autoexec ceiling, by construction.
        approval_required = (
            policy.requires_human
            or effective_tier >= _TIER_REQUIRES_APPROVAL
            or effective_tier > self.autoexec_max_tier
        )
        if approval_required:
            gate = approval_gate or self.approval_gate
            if gate is None:
                raise RuntimePolicyViolation(
                    f"Tool '{tool_name}' requires human approval but no approval gate is configured"
                )
            definition = approval_definition or RuntimeApprovalDefinition(
                name=tool_name,
                tier=effective_tier,
                mutates_state=action_mutates,
                requires_network=bool(requires_network),
            )
            gate(definition, tool_args)

        return RuntimePolicyDecision(
            tool_name=tool_name, policy=policy, approval_required=approval_required
        )

    def _enforce_command(self, command: str) -> None:
        if self.sandbox_policy is None:
            raise RuntimePolicyViolation("Bash requires a SandboxPolicy")
        violation = check_command(command, self.sandbox_policy)
        if violation:
            raise RuntimePolicyViolation(violation)
        try:
            # Inspect the REAL argv (unwrapped from bash -c / sh -c / env / sudo),
            # not the outer shlex tokens. Otherwise the -c payload stays a single
            # opaque token that resolves inside the workspace and slips past the
            # boundary check (2026-05-29 audit CRITICAL).
            tokens = _unwrap_command_tokens(shlex.split(command)) if command else []
        except ValueError as exc:
            raise RuntimePolicyViolation("unparseable command") from exc
        for token in tokens:
            path_token = _path_candidate_token(token)
            if path_token is None:
                continue
            if not _is_path_token(path_token, self.sandbox_policy):
                continue
            normalized = _resolve_path_for_policy(Path(path_token), self.sandbox_policy)
            roots = [
                root.expanduser().resolve(strict=False)
                for root in _path_roots(self.sandbox_policy, _SYSTEM_ROOTS)
            ]
            if not any(_is_relative_to(normalized, root) for root in roots):
                raise RuntimePolicyViolation("command references path outside allowed boundaries")
            if path_is_secret(normalized) or path_is_secret(path_token):
                raise RuntimePolicyViolation("command references a secret path")
        protected_branch = _protected_branch_for_git_commit(command, self.workspace_root)
        if protected_branch is not None:
            raise RuntimePolicyViolation(
                "git commit on protected branch "
                f"'{protected_branch}' is not allowed; use a feature branch or detached worktree"
            )

    def _enforce_paths(self, tool_name: str, args: dict, policy: ToolPolicy) -> None:
        for key, raw_path in _iter_path_values(args):
            self._validate_path_value(tool_name, key, raw_path, policy)

    def _validate_path_value(
        self, tool_name: str, key: str, raw_path: str, policy: ToolPolicy
    ) -> Path:
        if not raw_path.strip():
            return self.workspace_root
        decoded_path = _decode_path_text(raw_path)
        try:
            candidate = Path(decoded_path).expanduser()
        except RuntimeError:
            # LOW (2026-06-12): "~nonexistentuser/..." raises RuntimeError,
            # which escaped the structured deny. Fail closed instead.
            raise RuntimePolicyViolation(
                f"Tool '{tool_name}' path '{key}' could not be expanded"
            ) from None
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        resolved = candidate.resolve(strict=False)
        if (
            path_is_secret(decoded_path)
            or path_is_secret(resolved)
            or _blocked_by_policy(decoded_path, resolved, policy)
        ):
            raise RuntimePolicyViolation(
                f"Tool '{tool_name}' may not access secret path in '{key}'"
            )

        allowed_roots = self._allowed_roots_for_policy(policy)
        if not any(_is_relative_to(resolved, root) for root in allowed_roots):
            raise RuntimePolicyViolation(
                f"Tool '{tool_name}' path '{key}' is outside allowed roots"
            )
        return resolved

    def _allowed_roots_for_policy(self, policy: ToolPolicy) -> list[Path]:
        if policy.allowed_paths:
            roots = [
                _expand_policy_root(value, self.workspace_root) for value in policy.allowed_paths
            ]
        elif self.sandbox_policy is not None:
            roots = _path_roots(self.sandbox_policy)
        else:
            roots = [self.workspace_root]
        resolved: list[Path] = []
        for root in roots:
            root_path = root.expanduser().resolve(strict=False)
            if root_path not in resolved:
                resolved.append(root_path)
        return resolved

    def _enforce_network(
        self,
        tool_name: str,
        args: dict,
        policy: ToolPolicy,
        *,
        context: str,
        requires_network: bool,
    ) -> None:
        urls = list(_iter_urls(args))
        if requires_network:
            urls.extend(url for url in _DEFAULT_TOOL_URLS.get(tool_name, ()) if url not in urls)
        if not urls and not requires_network:
            return
        if not urls:
            return
        enforcer = self.network_enforcer or DomainAllowlistEnforcer()
        network_policy = NetworkPolicy(allowed_domains=list(policy.allowed_domains))
        for url in urls:
            decision = enforcer.enforce_url(url, policy=network_policy, actor=context)
            if not decision.allowed:
                raise RuntimePolicyViolation(
                    f"Tool '{tool_name}' network target blocked: {decision.reason}"
                )


def _tier_for_policy(policy: ToolPolicy) -> int:
    if policy.requires_human:
        return 3
    if policy.read_only:
        return 1
    return 2


@dataclass(frozen=True)
class _GitCommitTarget:
    """The repo a `git commit` will actually write to (not just the cwd)."""

    cwd: Path
    git_dir: Path | None
    work_tree: Path | None


def _protected_branch_for_git_commit(command: str, workspace_root: Path) -> str | None:
    target = _git_commit_target_from_command(command, workspace_root)
    if target is None:
        return None
    branch = _current_git_branch(target)
    if branch in _PROTECTED_GIT_COMMIT_BRANCHES:
        return branch
    return None


def _git_commit_target_from_command(command: str, workspace_root: Path) -> _GitCommitTarget | None:
    try:
        raw_tokens = shlex.split(command)
    except ValueError:
        return None
    tokens = _unwrap_command_tokens(raw_tokens)

    # Defense-in-depth: a `git commit` can target a DIFFERENT repo/branch than
    # the workspace via --git-dir/--work-tree flags or GIT_DIR/GIT_WORK_TREE env
    # assignments. Capture those so the protected-branch check inspects the repo
    # the commit will actually write to, not just the workspace cwd. Scan both
    # the raw and unwrapped token streams so the env form is caught whether it is
    # a bare prefix (`GIT_DIR=… git commit`), an `env GIT_DIR=… git commit`, or
    # wrapped in `bash -c "…"`. Out of scope by design (real boundary = triple-
    # AND gating + approvals, not this parser): compound commands (&&/;),
    # aliases, and shell-expansion forms like `git$IFS`commit.
    env_git_dir: str | None = None
    env_work_tree: str | None = None
    for token in (*raw_tokens, *tokens):
        if token.startswith("-") or "=" not in token:
            continue
        key, _, value = token.partition("=")
        if not key.isidentifier():
            continue
        if key == "GIT_DIR":
            env_git_dir = value
        elif key == "GIT_WORK_TREE":
            env_work_tree = value

    # Drop a leading bare env-assignment prefix so tokens[0] is the program.
    while (
        tokens
        and not tokens[0].startswith("-")
        and "=" in tokens[0]
        and tokens[0].partition("=")[0].isidentifier()
    ):
        tokens = tokens[1:]

    if not tokens or Path(tokens[0]).name != "git":
        return None

    cwd = workspace_root
    flag_git_dir: str | None = None
    flag_work_tree: str | None = None
    args = tokens[1:]
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-C":
            if index + 1 >= len(args):
                return None
            cwd = _resolve_git_cwd(cwd, args[index + 1])
            index += 2
            continue
        if arg.startswith("-C") and len(arg) > 2:
            cwd = _resolve_git_cwd(cwd, arg[2:])
            index += 1
            continue
        if arg == "--git-dir":
            if index + 1 >= len(args):
                return None
            flag_git_dir = args[index + 1]
            index += 2
            continue
        if arg.startswith("--git-dir="):
            flag_git_dir = arg[len("--git-dir=") :]
            index += 1
            continue
        if arg == "--work-tree":
            if index + 1 >= len(args):
                return None
            flag_work_tree = args[index + 1]
            index += 2
            continue
        if arg.startswith("--work-tree="):
            flag_work_tree = arg[len("--work-tree=") :]
            index += 1
            continue
        if arg in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if any(arg.startswith(prefix) for prefix in _GIT_GLOBAL_OPTIONS_WITH_ATTACHED_VALUE):
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        if arg != "commit":
            return None
        git_dir = flag_git_dir if flag_git_dir is not None else env_git_dir
        work_tree = flag_work_tree if flag_work_tree is not None else env_work_tree
        return _GitCommitTarget(
            cwd=cwd,
            git_dir=_resolve_git_cwd(cwd, git_dir) if git_dir else None,
            work_tree=_resolve_git_cwd(cwd, work_tree) if work_tree else None,
        )
    return None


def _resolve_git_cwd(current: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = current / candidate
    return candidate.resolve(strict=False)


def _git_target_argv(target: _GitCommitTarget) -> list[str]:
    argv = ["git", "-C", str(target.cwd)]
    if target.git_dir is not None:
        argv += ["--git-dir", str(target.git_dir)]
    if target.work_tree is not None:
        argv += ["--work-tree", str(target.work_tree)]
    return argv


def _current_git_branch(target: _GitCommitTarget) -> str | None:
    try:
        completed = subprocess.run(
            [*_git_target_argv(target), "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_GIT_BRANCH_CHECK_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimePolicyViolation("unable to verify git branch before commit") from exc
    if completed.returncode == 0:
        branch = completed.stdout.strip()
        return branch or None
    return None


def _context_candidates(context: str) -> set[str]:
    normalized = (context or "default").strip().lower()
    candidates = {normalized}
    if normalized in {"default", "brain", "worker", "worker_heavy", "operator"}:
        candidates.add("operator")
    if normalized in {"research", "researcher", "verifier", "judge"}:
        candidates.update({"research", "researcher"})
    if normalized in {"deploy", "deployer"}:
        candidates.add("deployer")
    if normalized in {"interactive", "telegram"}:
        candidates.add("telegram")
    if normalized in {"daemon", "scheduler", "kairos"}:
        candidates.add("daemon")
    return candidates


def _iter_path_values(value: Any, *, key: str = "") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        values: list[tuple[str, str]] = []
        for child_key, child_value in value.items():
            normalized = str(child_key).lower()
            if normalized in _PATH_KEYS and isinstance(child_value, (str, Path)):
                values.append((normalized, str(child_value)))
                continue
            # Recurse into anything else (dict/list, including non-scalars nested
            # under a path key) so a path arg buried in a container cannot bypass
            # the secret/boundary check.
            values.extend(_iter_path_values(child_value, key=normalized))
        return values
    if isinstance(value, list):
        values = []
        for item in value:
            values.extend(_iter_path_values(item, key=key))
        return values
    return [(key, str(value))] if key in _PATH_KEYS and isinstance(value, (str, Path)) else []


def _iter_urls(value: Any, *, key: str = "") -> list[str]:
    if isinstance(value, dict):
        urls: list[str] = []
        for child_key, child_value in value.items():
            normalized = str(child_key).lower()
            if normalized in _URL_KEYS and isinstance(child_value, str):
                if _looks_like_url(child_value) or _has_explicit_url_scheme(
                    child_value,
                    key=normalized,
                ):
                    urls.append(child_value)
                continue
            if normalized in _URL_LIST_KEYS and isinstance(child_value, list):
                urls.extend(str(item) for item in child_value if isinstance(item, str))
                continue
            if isinstance(child_value, (dict, list)):
                urls.extend(_iter_urls(child_value, key=normalized))
        return urls
    if isinstance(value, list):
        urls = []
        for item in value:
            urls.extend(_iter_urls(item, key=key))
        return urls
    if isinstance(value, str):
        if key in _URL_KEYS:
            return [value]
        return []
    return []


def _looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _has_explicit_url_scheme(value: str, *, key: str) -> bool:
    parsed = urlparse(value)
    scheme = (parsed.scheme or "").lower()
    if not scheme:
        return False
    if key == "target":
        return "://" in value or scheme in {
            "file",
            "chrome",
            "chrome-extension",
            "data",
            "javascript",
            "about",
            "ftp",
        }
    return True


def _blocked_by_policy(raw_path: str, resolved: Path, policy: ToolPolicy) -> bool:
    if not policy.blocked_path_patterns:
        return False
    candidates = (raw_path, str(resolved), resolved.name)
    return any(
        fnmatch.fnmatch(candidate, pattern)
        for pattern in policy.blocked_path_patterns
        for candidate in candidates
    )


def _expand_policy_root(value: str, workspace_root: Path) -> Path:
    raw = str(value)
    if raw == "WORKSPACE_ROOT":
        return workspace_root
    if raw == "HOME":
        return Path.home()
    if raw.startswith("HOME/"):
        return Path.home() / raw.removeprefix("HOME/")
    if raw == "CLAW_HOME":
        return Path.home() / ".claw"
    if raw.startswith("CLAW_HOME/"):
        return Path.home() / ".claw" / raw.removeprefix("CLAW_HOME/")
    if raw == "TMP":
        return Path("/private/tmp")
    path = Path(raw)
    return path if path.is_absolute() else workspace_root / path


def _path_roots(policy: SandboxPolicy, system_roots: list[Path] | None = None) -> list[Path]:
    return [
        policy.workspace_root,
        *policy.allowed_paths,
        *policy.writable_paths,
        *(system_roots or []),
    ]


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


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
