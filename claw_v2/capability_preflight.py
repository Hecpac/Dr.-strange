from __future__ import annotations

import shlex
import shutil
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from claw_v2.sandbox import SandboxPolicy, check_command


@dataclass(slots=True)
class CommandSpec:
    command: str
    purpose: str
    requires_network: bool = False
    requires_sudo: bool = False
    requires_gui: bool = False
    risk_tier: str = "tier_2"


@dataclass(slots=True)
class CommandPreflight:
    command: str
    binary: str
    purpose: str
    status: str
    exists: bool
    policy_allowed: bool
    blocker: str = ""
    requires_network: bool = False
    requires_sudo: bool = False
    requires_gui: bool = False
    risk_tier: str = "tier_2"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class CapabilityPreflightResult:
    task_kind: str
    risk_tier: str
    plan: list[str]
    current_step: str
    verification_requirement: str
    checks: list[CommandPreflight]
    blockers: list[str]
    requires_network: bool = False
    requires_sudo: bool = False
    requires_gui: bool = False

    @property
    def allowed(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["allowed"] = self.allowed
        return payload


WhichFunc = Callable[[str], str | None]


def preflight_objective(
    objective: str,
    *,
    workspace_root: Path | str,
    capability_profile: str = "engineer",
    which: WhichFunc | None = None,
    allowed_binaries: set[str] | None = None,
) -> CapabilityPreflightResult:
    task_kind, specs = command_specs_for_objective(objective)
    policy = SandboxPolicy(
        workspace_root=Path(workspace_root),
        capability_profile=capability_profile,
        allowed_binaries=allowed_binaries,
    )
    which = which or shutil.which
    checks = [preflight_command(spec, policy=policy, which=which) for spec in specs]
    blockers = [check.blocker for check in checks if check.blocker]
    risk_tier = _max_risk_tier([spec.risk_tier for spec in specs]) if specs else "tier_1"
    return CapabilityPreflightResult(
        task_kind=task_kind,
        risk_tier=risk_tier,
        plan=plan_for_task_kind(task_kind),
        current_step="capability_preflight",
        verification_requirement=verification_requirement_for_task_kind(task_kind),
        checks=checks,
        blockers=blockers,
        requires_network=any(spec.requires_network for spec in specs),
        requires_sudo=any(spec.requires_sudo for spec in specs),
        requires_gui=any(spec.requires_gui for spec in specs),
    )


def preflight_command(
    spec: CommandSpec,
    *,
    policy: SandboxPolicy,
    which: WhichFunc | None = None,
) -> CommandPreflight:
    which = which or shutil.which
    binary = _binary_for_command(spec.command)
    exists = bool(binary and which(binary))
    violation = check_command(spec.command, policy)
    if not exists:
        return CommandPreflight(
            command=spec.command,
            binary=binary,
            purpose=spec.purpose,
            status="command_not_found",
            exists=False,
            policy_allowed=False,
            blocker=f"command_not_found:{binary}",
            requires_network=spec.requires_network,
            requires_sudo=spec.requires_sudo,
            requires_gui=spec.requires_gui,
            risk_tier=spec.risk_tier,
        )
    if violation:
        return CommandPreflight(
            command=spec.command,
            binary=binary,
            purpose=spec.purpose,
            status="policy_blocked",
            exists=True,
            policy_allowed=False,
            blocker=f"policy_blocked:{binary}:{violation}",
            requires_network=spec.requires_network,
            requires_sudo=spec.requires_sudo,
            requires_gui=spec.requires_gui,
            risk_tier=spec.risk_tier,
        )
    return CommandPreflight(
        command=spec.command,
        binary=binary,
        purpose=spec.purpose,
        status="allowed",
        exists=True,
        policy_allowed=True,
        requires_network=spec.requires_network,
        requires_sudo=spec.requires_sudo,
        requires_gui=spec.requires_gui,
        risk_tier=spec.risk_tier,
    )


def command_specs_for_objective(objective: str) -> tuple[str, list[CommandSpec]]:
    normalized = _normalize(objective)
    if _looks_like_tool_update(normalized):
        return (
            "maintenance_update_tools",
            [
                CommandSpec("codex --version", "codex_cli_version_check", requires_network=True),
                CommandSpec("claude --version", "claude_code_version_check", requires_network=True),
                CommandSpec("npm --version", "node_package_manager_check", requires_network=True),
                CommandSpec(
                    "osascript -e 'id of app \"Codex\"'", "codex_app_gui_check", requires_gui=True
                ),
            ],
        )
    if _looks_like_lock_regeneration(normalized):
        return (
            "qts_lock_regeneration",
            [
                CommandSpec("python3 --version", "python_runtime_check"),
                CommandSpec("poetry --version", "poetry_runtime_check"),
                CommandSpec("git status --short", "worktree_state_check"),
            ],
        )
    if _looks_like_pr_completion(normalized):
        return (
            "pull_request_completion",
            [
                CommandSpec("git status --short", "worktree_state_check"),
                CommandSpec("gh pr status", "pull_request_state_check", requires_network=True),
                CommandSpec("python3 --version", "python_runtime_check"),
            ],
        )
    if _looks_like_test_execution(normalized):
        return (
            "local_test_execution",
            [
                CommandSpec("python3 -m pytest --version", "pytest_runtime_check"),
            ],
        )
    return ("generic_autonomous_task", [])


def plan_for_task_kind(task_kind: str) -> list[str]:
    plans = {
        "maintenance_update_tools": [
            "preflight tool availability and policy",
            "check installed versions",
            "run only allowed update checks or record blockers",
            "verify version/output evidence",
        ],
        "qts_lock_regeneration": [
            "preflight Python, Poetry, and git access",
            "inspect pyproject/lock drift",
            "regenerate lock only if tooling is allowed",
            "run focused verification or record blocker evidence",
        ],
        "pull_request_completion": [
            "preflight git, gh, and test tooling",
            "inspect PR/worktree state",
            "finish local changes within approval policy",
            "verify and report remaining blockers",
        ],
        "local_test_execution": [
            "preflight test runner",
            "run focused test command",
            "record pass/fail evidence",
        ],
        "generic_autonomous_task": [
            "create durable task",
            "run coordinator within autonomy policy",
            "verify outcome before reporting completion",
        ],
    }
    return list(plans.get(task_kind, plans["generic_autonomous_task"]))


def verification_requirement_for_task_kind(task_kind: str) -> str:
    requirements = {
        "maintenance_update_tools": "version checks or concrete tool blockers",
        "qts_lock_regeneration": "poetry.lock regenerated and tests/checks run, or blocker evidence",
        "pull_request_completion": "PR/worktree status plus tests or concrete blockers",
        "local_test_execution": "test command output",
        "generic_autonomous_task": "coordinator evidence and verification status",
    }
    return requirements.get(task_kind, requirements["generic_autonomous_task"])


def _binary_for_command(command: str) -> str:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    if not tokens:
        return ""
    return Path(tokens[0]).name


def _looks_like_tool_update(normalized: str) -> bool:
    mentions_tool = any(
        token in normalized for token in ("codex", "claude", "claude code", "codex app")
    )
    asks_update = any(
        token in normalized for token in ("actualiza", "actualizar", "update", "upgrade")
    )
    contextual_followup = any(
        phrase in normalized
        for phrase in (
            "actualizalas tu",
            "actualizalos tu",
            "debes actualizarlas",
            "debes actualizarlos",
        )
    )
    return (mentions_tool and asks_update) or contextual_followup


def _looks_like_lock_regeneration(normalized: str) -> bool:
    return (
        (
            "lock" in normalized
            and any(token in normalized for token in ("regenera", "regenerar", "poetry", "qts"))
        )
        or "poetry.lock" in normalized
        or ("pyproject" in normalized and "lock" in normalized)
    )


def _looks_like_pr_completion(normalized: str) -> bool:
    return "pr" in normalized and any(
        token in normalized for token in ("termina", "completa", "cierra", "finaliza")
    )


def _looks_like_test_execution(normalized: str) -> bool:
    return any(token in normalized for token in ("pytest", "test", "prueba"))


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in folded if not unicodedata.combining(ch)).lower()


def _max_risk_tier(values: list[str]) -> str:
    order = {"tier_1": 1, "tier_2": 2, "tier_3": 3}
    highest = max(values, key=lambda value: order.get(value, 2), default="tier_2")
    return highest if highest in order else "tier_2"
