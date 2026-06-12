"""Success condition + preflight + audit envelope for the verifier (F1+F2).

Introduced 2026-05-26 to close the class of false positives where a tool
returned ok=True but the external state was not actually what the task required
(see MEMORY.md: X compose-thread orphan T8, LinkedIn handle stale).

Design constraints from Hector 2026-05-26:
- tool.ok=True alone NEVER marks a task succeeded if a SuccessCondition exists.
- Tier 3 succeeded requires preflight + external_check validated + evidence.
- Tier 2 local succeeded requires state_delta_check to pass fully.
- Memory validators are READ-ONLY (in claw_v2/memory_revalidation.py).

This module is the data layer + pure-function evaluator. F1+F2 only:
nothing here touches the runtime task ledger, real tools, or external services.
"""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


SUCCESS_CONDITION_SCHEMA_VERSION = "1.0.0"

# D9 (2026-06-12): bounds for must_match_regex evaluation. The gate runs on
# untrusted artifact data; a catastrophic-backtracking pattern (e.g. (a+)+$)
# over a large value would hang the promote path. Patterns with quantified
# groups that themselves contain quantifiers or alternation are rejected as
# regex_invalid rather than risk exponential search.
_REGEX_INPUT_CAP_CHARS = 10_000
_REGEX_PATTERN_CAP_CHARS = 512
_NESTED_QUANTIFIER_RE = re.compile(r"\([^)]*[+*|][^)]*\)\s*[+*{]")


# ---------------------------------------------------------------------------
# Declarative specs
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ExternalCheckSpec:
    kind: Literal[
        "cdp_phrase",
        "http_get_json",
        "filesystem_glob",
        "gh_api",
        "sqlite_row",
    ]
    target: str
    expected_phrases: tuple[str, ...] = ()
    forbidden_phrases: tuple[str, ...] = ()
    json_path_equals: dict[str, Any] | None = None
    settle_seconds: float = 6.0
    timeout_seconds: float = 30.0
    retries: int = 3


@dataclass(slots=True, frozen=True)
class FileIntegrityCheck:
    """F3a-ext.1 — Re-hash + re-size a file produced by the tool and assert
    the values match the declared ones. Reads at most `max_bytes` from disk.
    """
    path_field: str                                       # key in tool_result holding the local path
    hash_field: str = ""                                  # key holding the expected sha256 (64 hex)
    size_field: str = ""                                  # key holding the expected size in bytes
    max_bytes: int = 64 * 1024 * 1024                     # safety cap: don't hash huge files


@dataclass(slots=True, frozen=True)
class StateDeltaSpec:
    db_table: str | None = None
    expected_rows_delta: int = 1
    fs_path: str | None = None
    expected_size_delta_bytes: int = 1
    # F3a.2 — Edit-class tools require post-state content hash to differ from
    # pre-state. Default False keeps existing Write/Bash contracts unchanged.
    expected_content_changed: bool = False


@dataclass(slots=True, frozen=True)
class SuccessCondition:
    must_contain_keys: tuple[str, ...] = ()
    must_match_regex: dict[str, str] = field(default_factory=dict)
    external_check: ExternalCheckSpec | None = None
    state_delta_check: StateDeltaSpec | None = None
    forbidden_reasons: tuple[str, ...] = ()
    schema_version: str = SUCCESS_CONDITION_SCHEMA_VERSION
    # F3a.2 — exact-equality assertions on result fields (e.g. exit_code==0).
    must_equal: dict[str, Any] = field(default_factory=dict)
    # F3a.2 — keys that MUST be present AND empty (e.g. issues=[] for a clean
    # WikiLint pass). Distinct from must_contain_keys (presence only).
    must_be_empty_keys: tuple[str, ...] = ()
    # F3a-extension — result keys whose values are local filesystem paths
    # that MUST exist and have size > 0. Used by SkillGenerate to verify the
    # generated file actually landed.
    must_be_existing_path: tuple[str, ...] = ()
    # F3a-ext.1 — semantic hardening (offline-safe).
    must_be_nonempty_str: tuple[str, ...] = ()             # value must be non-blank str
    cross_field_equality: tuple[tuple[str, str], ...] = () # (a, b) → result[a] == result[b]
    cross_field_inequality: tuple[tuple[str, str], ...] = ()  # (a, b) → result[a] != result[b]
    forbidden_field_values: dict[str, tuple[str, ...]] = field(default_factory=dict)
    verify_file_integrity: tuple[FileIntegrityCheck, ...] = ()
    # Allowed root paths for must_be_existing_path / verify_file_integrity.
    # Empty means "no constraint" (back-compat). Runtime injects safe roots.
    allowed_path_roots: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class PreflightSpec:
    probe_kind: Literal[
        "dom_selector_present",
        "auth_check",
        "account_exists",
        "branch_check",
        "dry_run",
    ]
    target: str
    selectors: tuple[str, ...] = ()
    must_match: dict[str, Any] = field(default_factory=dict)
    fail_message: str = "preflight failed"


# ---------------------------------------------------------------------------
# Audit envelope
# ---------------------------------------------------------------------------


VerificationStatus = Literal["passed", "pending_verification", "failed", "blocked"]


@dataclass(slots=True)
class VerificationResult:
    status: VerificationStatus
    success_condition_version: str
    validated_at: float
    validated_by: str
    evidence_uri: str | None
    verification_result: dict

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Pure-function evaluator
# ---------------------------------------------------------------------------


def validate_success_condition(
    *,
    tool_result: dict,
    condition: SuccessCondition,
    external_observation: dict | None = None,
    state_delta_observation: dict | None = None,
) -> list[str]:
    """Pure function. Returns a list of error codes; empty list = satisfied.

    Does NOT call out to any external service. Callers must pre-fetch the
    observations (or stub them in tests) before invoking.
    """
    errors: list[str] = []

    if not bool(tool_result.get("ok")):
        errors.append("tool_not_ok")
        return errors

    reason = str(tool_result.get("reason") or "")
    for bad in condition.forbidden_reasons:
        if reason and bad and bad == reason:
            errors.append(f"forbidden_reason_matched:{bad}")

    # Presence-based semantics (key MUST be present in the result dict).
    # Truthy/non-empty assertions belong in `must_match_regex` or a future
    # `must_be_non_empty` field — this avoids false negatives on legitimate
    # zero-valued results like `exit_code=0` (Bash success) or `issues=[]`
    # (clean WikiLint pass).
    for key in condition.must_contain_keys:
        if key not in tool_result:
            errors.append(f"missing_key:{key}")

    for field_name, pattern in (condition.must_match_regex or {}).items():
        # D9 (2026-06-12): the gate is a pure function on untrusted artifact
        # data — cap the evaluated text and reject dangerous patterns instead
        # of letting a catastrophic-backtracking regex hang the promote path.
        value = str(tool_result.get(field_name) or "")[:_REGEX_INPUT_CAP_CHARS]
        if len(pattern) > _REGEX_PATTERN_CAP_CHARS or _NESTED_QUANTIFIER_RE.search(pattern):
            errors.append(f"regex_invalid:{field_name}")
            continue
        try:
            if not re.search(pattern, value):
                errors.append(f"regex_mismatch:{field_name}")
        except re.error:
            errors.append(f"regex_invalid:{field_name}")

    if condition.external_check is not None:
        obs = external_observation or {}
        if not obs:
            errors.append("external_check_required_but_no_observation")
        else:
            body_text = str(obs.get("body_text") or "")
            for phrase in condition.external_check.expected_phrases:
                if phrase not in body_text:
                    errors.append(f"expected_phrase_missing:{phrase}")
            for bad in condition.external_check.forbidden_phrases:
                if bad and bad in body_text:
                    errors.append(f"forbidden_phrase_present:{bad}")
            for path, expected in (condition.external_check.json_path_equals or {}).items():
                actual = obs.get("json", {}).get(path) if isinstance(obs.get("json"), dict) else None
                if actual != expected:
                    errors.append(f"json_path_mismatch:{path}")

    if condition.state_delta_check is not None:
        obs = state_delta_observation or {}
        if not obs:
            errors.append("state_delta_required_but_no_observation")
        else:
            if condition.state_delta_check.db_table:
                rows = int(obs.get("rows_added") or 0)
                if rows < condition.state_delta_check.expected_rows_delta:
                    errors.append("state_delta_rows_below_threshold")
            if condition.state_delta_check.fs_path:
                size = int(obs.get("fs_size_added_bytes") or 0)
                if size < condition.state_delta_check.expected_size_delta_bytes:
                    errors.append("state_delta_fs_size_below_threshold")
            # F3a.2 — Edit-class tools must show observable content change.
            if condition.state_delta_check.expected_content_changed:
                if not bool(obs.get("content_changed")):
                    errors.append("state_delta_content_unchanged")

    # F3a.2 — must_equal (exact equality on result fields, e.g. exit_code==0)
    for key, expected in (condition.must_equal or {}).items():
        if tool_result.get(key) != expected:
            errors.append(f"must_equal_mismatch:{key}")

    # F3a.2 — must_be_empty_keys (key must be present AND empty)
    for key in condition.must_be_empty_keys:
        if key not in tool_result:
            errors.append(f"missing_key:{key}")
            continue
        value = tool_result[key]
        try:
            if len(value) != 0:
                errors.append(f"must_be_empty_violated:{key}")
        except TypeError:
            # Not a container — non-empty by definition
            errors.append(f"must_be_empty_violated:{key}")

    # F3a-extension — must_be_existing_path: each named result key carries a
    # filesystem path that must exist locally and have size > 0. Used by
    # SkillGenerate to assert the generated artifact really landed.
    import hashlib
    import os

    def _path_within_allowed_roots(candidate: str) -> bool:
        if not condition.allowed_path_roots:
            return True
        try:
            real = os.path.realpath(candidate)
        except (OSError, ValueError):
            return False
        for root in condition.allowed_path_roots:
            try:
                root_real = os.path.realpath(root)
            except (OSError, ValueError):
                continue
            if real == root_real or real.startswith(root_real.rstrip("/") + "/"):
                return True
        return False

    for key in (condition.must_be_existing_path or ()):
        path_value = tool_result.get(key)
        if not isinstance(path_value, str) or not path_value:
            errors.append(f"path_field_missing:{key}")
            continue
        if not _path_within_allowed_roots(path_value):
            errors.append(f"path_outside_allowed_root:{key}")
            continue
        try:
            stat = os.stat(path_value)
            if stat.st_size <= 0:
                errors.append(f"path_file_empty:{key}")
        except (OSError, FileNotFoundError):
            errors.append(f"path_file_not_found:{key}")

    # F3a-ext.1 — must_be_nonempty_str (rejects "", "   ", whitespace-only)
    for key in (condition.must_be_nonempty_str or ()):
        value = tool_result.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"must_be_nonempty_str_violated:{key}")

    # F3a-ext.1 — cross-field equality / inequality
    for a, b in (condition.cross_field_equality or ()):
        if a not in tool_result or b not in tool_result:
            errors.append(f"cross_field_equality_missing:{a}={b}")
            continue
        if tool_result[a] != tool_result[b]:
            errors.append(f"cross_field_equality_violated:{a}={b}")
    for a, b in (condition.cross_field_inequality or ()):
        if a not in tool_result or b not in tool_result:
            errors.append(f"cross_field_inequality_missing:{a}!={b}")
            continue
        if tool_result[a] == tool_result[b]:
            errors.append(f"cross_field_inequality_violated:{a}!={b}")

    # F3a-ext.1 — forbidden_field_values (case-insensitive string compare)
    for key, banned in (condition.forbidden_field_values or {}).items():
        value = tool_result.get(key)
        if value is None:
            continue
        banned_lc = {str(b).strip().lower() for b in banned}
        if str(value).strip().lower() in banned_lc:
            errors.append(f"forbidden_field_value:{key}={value}")

    # F3a-ext.1 — verify_file_integrity (re-hash + re-size files; respects allowed_path_roots)
    for check in (condition.verify_file_integrity or ()):
        path = tool_result.get(check.path_field)
        if not isinstance(path, str) or not path:
            errors.append(f"integrity_path_field_missing:{check.path_field}")
            continue
        if not _path_within_allowed_roots(path):
            errors.append(f"integrity_path_outside_allowed_root:{check.path_field}")
            continue
        try:
            stat = os.stat(path)
            actual_size = stat.st_size
            if actual_size > check.max_bytes:
                errors.append(f"integrity_file_exceeds_max_bytes:{check.path_field}")
                continue
        except (OSError, FileNotFoundError):
            errors.append(f"integrity_file_not_found:{check.path_field}")
            continue
        if check.size_field:
            declared_size = tool_result.get(check.size_field)
            if isinstance(declared_size, int) and declared_size != actual_size:
                errors.append(f"integrity_size_mismatch:{check.path_field}")
        if check.hash_field:
            declared_hash = tool_result.get(check.hash_field)
            try:
                h = hashlib.sha256()
                with open(path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(8192), b""):
                        h.update(chunk)
                actual_hash = h.hexdigest()
                if isinstance(declared_hash, str) and declared_hash.lower() != actual_hash.lower():
                    errors.append(f"integrity_hash_mismatch:{check.path_field}")
            except (OSError, FileNotFoundError):
                errors.append(f"integrity_hash_read_failed:{check.path_field}")

    return errors


def build_verification_envelope(
    *,
    condition: SuccessCondition,
    errors: list[str],
    evidence_uri: str | None,
    validated_by: str = "verifier",
    external_observation: dict | None = None,
) -> VerificationResult:
    if not errors:
        status: VerificationStatus = "passed"
    elif "tool_not_ok" in errors:
        status = "failed"
    elif any(e.startswith("forbidden_reason_matched") for e in errors):
        status = "failed"
    elif "external_check_required_but_no_observation" in errors:
        status = "pending_verification"
    elif "state_delta_required_but_no_observation" in errors:
        status = "pending_verification"
    else:
        status = "failed"

    obs_sha = ""
    if external_observation is not None:
        obs_sha = hashlib.sha256(
            repr(sorted(external_observation.items())).encode("utf-8")
        ).hexdigest()[:16]

    return VerificationResult(
        status=status,
        success_condition_version=condition.schema_version,
        validated_at=time.time(),
        validated_by=validated_by,
        evidence_uri=evidence_uri,
        verification_result={
            "errors": list(errors),
            "external_observation_sha": obs_sha,
            "verification_id": uuid.uuid4().hex[:12],
        },
    )


# ---------------------------------------------------------------------------
# Registration-time helpers (warn-only in F1)
# ---------------------------------------------------------------------------


class ToolContractWarning(UserWarning):
    """F1 emits this when a tier>=2 tool lacks success_condition or a tier 3
    tool lacks preflight. F4 will convert to a hard ToolRegistrationError."""


def warn_if_contract_missing(
    *,
    tool_name: str,
    tier: int,
    has_sc: bool,
    has_pf: bool,
) -> str | None:
    if tier >= 2 and not has_sc:
        return (
            f"tool {tool_name!r} tier={tier} has no success_condition — "
            "tool.ok=True alone will not be sufficient evidence (F2 contract). "
            "Will become a hard error in F4."
        )
    if tier == 3 and not has_pf:
        return (
            f"tool {tool_name!r} is Tier 3 but has no preflight — "
            "Tier 3 actions must declare a preflight probe (DOM/auth/account/branch). "
            "Will become a hard error in F4."
        )
    return None
