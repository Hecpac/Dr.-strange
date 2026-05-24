from __future__ import annotations

import shlex
import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse

from claw_v2.network_proxy import DomainAllowlistEnforcer, NetworkPolicy
from claw_v2.sandbox import SandboxPolicy, check_command
from claw_v2.tool_policy import TOOL_POLICIES, ToolPolicy, _decode_path_text, path_is_secret

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
_SYSTEM_ROOTS = [Path("/usr"), Path("/bin"), Path("/sbin"), Path("/opt"), Path("/tmp"), Path("/private/tmp")]
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
        self.autoexec_max_tier = autoexec_max_tier
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
            raise RuntimePolicyViolation(f"Tool '{tool_name}' is not declared in tool_policies.json")

        contexts = _context_candidates(context)
        if not contexts.intersection(policy.allowed_contexts):
            allowed = ", ".join(sorted(policy.allowed_contexts)) or "<none>"
            raise RuntimePolicyViolation(
                f"Tool '{tool_name}' is not allowed in context '{context}' (allowed: {allowed})"
            )

        action_mutates = bool(mutates_state)
        if policy.read_only and action_mutates:
            raise RuntimePolicyViolation(f"Tool '{tool_name}' is read-only by policy but requested mutation")

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
        approval_required = policy.requires_human or effective_tier > self.autoexec_max_tier
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

        return RuntimePolicyDecision(tool_name=tool_name, policy=policy, approval_required=approval_required)

    def _enforce_command(self, command: str) -> None:
        if self.sandbox_policy is None:
            raise RuntimePolicyViolation("Bash requires a SandboxPolicy")
        violation = check_command(command, self.sandbox_policy)
        if violation:
            raise RuntimePolicyViolation(violation)
        try:
            tokens = shlex.split(command) if command else []
        except ValueError as exc:
            raise RuntimePolicyViolation("unparseable command") from exc
        for token in tokens:
            path_token = _path_candidate_token(token)
            if path_token is None:
                continue
            if not _is_path_token(path_token, self.sandbox_policy):
                continue
            normalized = _resolve_path_for_policy(Path(path_token), self.sandbox_policy)
            roots = [root.expanduser().resolve(strict=False) for root in _path_roots(self.sandbox_policy, _SYSTEM_ROOTS)]
            if not any(_is_relative_to(normalized, root) for root in roots):
                raise RuntimePolicyViolation("command references path outside allowed boundaries")
            if path_is_secret(normalized) or path_is_secret(path_token):
                raise RuntimePolicyViolation("command references a secret path")

    def _enforce_paths(self, tool_name: str, args: dict, policy: ToolPolicy) -> None:
        for key, raw_path in _iter_path_values(args):
            self._validate_path_value(tool_name, key, raw_path, policy)

    def _validate_path_value(self, tool_name: str, key: str, raw_path: str, policy: ToolPolicy) -> Path:
        if not raw_path.strip():
            return self.workspace_root
        decoded_path = _decode_path_text(raw_path)
        candidate = Path(decoded_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        resolved = candidate.resolve(strict=False)
        if path_is_secret(decoded_path) or path_is_secret(resolved) or _blocked_by_policy(decoded_path, resolved, policy):
            raise RuntimePolicyViolation(f"Tool '{tool_name}' may not access secret path in '{key}'")

        allowed_roots = self._allowed_roots_for_policy(policy)
        if not any(_is_relative_to(resolved, root) for root in allowed_roots):
            raise RuntimePolicyViolation(f"Tool '{tool_name}' path '{key}' is outside allowed roots")
        return resolved

    def _allowed_roots_for_policy(self, policy: ToolPolicy) -> list[Path]:
        if policy.allowed_paths:
            roots = [_expand_policy_root(value, self.workspace_root) for value in policy.allowed_paths]
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
                raise RuntimePolicyViolation(f"Tool '{tool_name}' network target blocked: {decision.reason}")


def _tier_for_policy(policy: ToolPolicy) -> int:
    if policy.requires_human:
        return 3
    if policy.read_only:
        return 1
    return 2


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
            if normalized in _PATH_KEYS:
                if isinstance(child_value, (str, Path)):
                    values.append((normalized, str(child_value)))
                continue
            if isinstance(child_value, dict):
                values.extend(_iter_path_values(child_value, key=normalized))
        return values
    return [(key, str(value))] if key in _PATH_KEYS and isinstance(value, (str, Path)) else []


def _iter_urls(value: Any, *, key: str = "") -> list[str]:
    if isinstance(value, dict):
        urls: list[str] = []
        for child_key, child_value in value.items():
            normalized = str(child_key).lower()
            if normalized in _URL_KEYS and isinstance(child_value, str):
                if _looks_like_url(child_value):
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


def _blocked_by_policy(raw_path: str, resolved: Path, policy: ToolPolicy) -> bool:
    if not policy.blocked_path_patterns:
        return False
    candidates = (raw_path, str(resolved), resolved.name)
    return any(fnmatch.fnmatch(candidate, pattern) for pattern in policy.blocked_path_patterns for candidate in candidates)


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


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
