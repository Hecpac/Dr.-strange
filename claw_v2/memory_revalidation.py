"""Read-only validators for memory load-bearing claims.

Constraint from Hector 2026-05-26 §7-§8:
  - Validators are READ-ONLY. They MUST NOT publish, edit, push, follow,
    send messages, mutate external systems, or write to MEMORY.md.
  - On stale, the validator returns a `proposed_patch` for human review and
    the caller BLOCKS the action. Auto-update of MEMORY.md is forbidden.

F1+F2 deliverable: registry + dataclasses + a few example validators that
are pure-function (accept observations passed in by callers / tests).
Real CDP / gh / API integrations land in F5 and will live in a *separate*
runner module so this registry itself stays mockable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Mapping


@dataclass(slots=True, frozen=True)
class MemoryClaimValidation:
    """Result of a single read-only memory claim check."""

    claim_key: str
    valid: bool
    reason: str  # short code, e.g. "ok", "account_does_not_exist"
    evidence: dict[str, Any] = field(default_factory=dict)
    proposed_patch: dict[str, Any] | None = None  # suggested correction for human review

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RevalidationOutcome:
    """Aggregate result across all requested claims."""

    all_valid: bool
    valid: dict[str, MemoryClaimValidation]
    invalid: dict[str, MemoryClaimValidation]
    block_action: bool  # True if any invalid claim → Tier 3 must abort
    proposed_patches: list[dict[str, Any]] = field(default_factory=list)


# A validator is a pure function:
#   (claimed_value: Any, observation: Mapping[str, Any]) -> MemoryClaimValidation
# Callers (F5) will pre-fetch the observation via CDP/gh/etc. and pass it in.
# In tests, observations are stubbed.
MemoryClaimValidator = Callable[[Any, Mapping[str, Any]], MemoryClaimValidation]


# ---------------------------------------------------------------------------
# Example validators (read-only; no side effects)
# ---------------------------------------------------------------------------


def validate_x_handle(claimed_handle: Any, observation: Mapping[str, Any]) -> MemoryClaimValidation:
    """Validate that an X handle still exists for the active session.

    observation expected shape (provided by F5 CDP runner; stubbed in tests):
      {"account_exists": bool, "fetched_url": str, "fetched_at": float}

    NEVER mutates anything. On stale, proposes a patch for MEMORY.md review.
    """
    handle = str(claimed_handle or "").lstrip("@")
    if not handle:
        return MemoryClaimValidation(
            claim_key="x_handle",
            valid=False,
            reason="claim_value_empty",
            evidence={"claimed": claimed_handle},
        )
    if observation.get("account_exists") is True:
        return MemoryClaimValidation(
            claim_key="x_handle",
            valid=True,
            reason="ok",
            evidence={"claimed": handle, "fetched_url": observation.get("fetched_url")},
        )
    return MemoryClaimValidation(
        claim_key="x_handle",
        valid=False,
        reason="account_does_not_exist",
        evidence={"claimed": handle, "fetched_url": observation.get("fetched_url")},
        proposed_patch={
            "memory_file": "MEMORY.md",
            "action": "human_review_required",
            "summary": (
                f"Memory claims X handle @{handle} but live check shows the "
                "account does not exist. Confirm the current handle and apply "
                "the correction manually before any Tier 3 X action."
            ),
        },
    )


def validate_github_repo(
    claimed_repo: Any, observation: Mapping[str, Any]
) -> MemoryClaimValidation:
    """observation: {'repo_exists': bool, 'default_branch': str | None}"""
    repo = str(claimed_repo or "")
    if "/" not in repo:
        return MemoryClaimValidation(
            claim_key="github_repo",
            valid=False,
            reason="claim_value_malformed",
            evidence={"claimed": claimed_repo},
        )
    if observation.get("repo_exists") is True:
        return MemoryClaimValidation(
            claim_key="github_repo",
            valid=True,
            reason="ok",
            evidence={"claimed": repo, "default_branch": observation.get("default_branch")},
        )
    return MemoryClaimValidation(
        claim_key="github_repo",
        valid=False,
        reason="repo_not_found",
        evidence={"claimed": repo},
        proposed_patch={
            "memory_file": "MEMORY.md",
            "action": "human_review_required",
            "summary": f"Repo {repo} not found via gh api. Confirm spelling/ownership.",
        },
    )


# ---------------------------------------------------------------------------
# Registry (mutable for test monkeypatching, NOT for production mutation)
# ---------------------------------------------------------------------------


_VALIDATORS: dict[str, MemoryClaimValidator] = {
    "x_handle": validate_x_handle,
    "github_repo": validate_github_repo,
}


def register_validator(claim_key: str, validator: MemoryClaimValidator) -> None:
    """Register or replace a validator. Used by tests via monkeypatch.
    Production code should use the module-level functions directly."""
    _VALIDATORS[claim_key] = validator


def get_validator(claim_key: str) -> MemoryClaimValidator | None:
    return _VALIDATORS.get(claim_key)


def revalidate_memory_claims(
    claim_keys: tuple[str, ...],
    *,
    context: Mapping[str, Any],
    observations: Mapping[str, Mapping[str, Any]] | None = None,
) -> RevalidationOutcome:
    """Run validators for the given claim_keys.

    Args:
      claim_keys: ordered tuple of claim names to check (e.g. ('x_handle',))
      context:   maps claim_key -> claimed_value from current state/memory
      observations: maps claim_key -> observation dict pre-fetched by the
                   F5 runner (in tests, stub this directly).

    BLOCKS the calling action if any claim returns valid=False.
    """
    obs = observations or {}
    valid: dict[str, MemoryClaimValidation] = {}
    invalid: dict[str, MemoryClaimValidation] = {}
    patches: list[dict[str, Any]] = []

    for key in claim_keys:
        validator = get_validator(key)
        if validator is None:
            v = MemoryClaimValidation(
                claim_key=key,
                valid=False,
                reason="no_validator_registered",
                evidence={"claimed": context.get(key)},
            )
            invalid[key] = v
            continue
        observation = obs.get(key, {})
        result = validator(context.get(key), observation)
        if result.valid:
            valid[key] = result
        else:
            invalid[key] = result
            if result.proposed_patch:
                patches.append(result.proposed_patch)

    return RevalidationOutcome(
        all_valid=not invalid,
        valid=valid,
        invalid=invalid,
        block_action=bool(invalid),
        proposed_patches=patches,
    )
