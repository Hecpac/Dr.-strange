"""F3a — Declarative SuccessCondition for local Tier-2 tools.

Scope (offline, no external tools):
  - Write       : writing to a file under WORKSPACE_ROOT
  - Edit        : applying a patch to a file under WORKSPACE_ROOT
  - Bash        : running an allowlisted shell command (state delta optional)
  - WikiLint    : auditing wiki health (read-only-ish; gains a contract so its
                  tier=2 registration loses the F1 warning)

Each contract pins exactly what the tool's `ok=True` result MUST carry, and
when applicable, what state delta MUST be observable locally (filesystem
size change for Write/Edit; nothing required for Bash because Bash may
legitimately produce stdout-only results).

GitCommit is intentionally NOT included here — there is no discrete
`ToolDefinition(name="GitCommit", ...)` in the current registry (git is
invoked via Bash), so wiring it would be premature. Same for WikiDelete
(Tier 3 — out of F3a scope per Hector's restrictions §1).

This module is pure-function. No I/O, no external calls. The helper
`build_local_tool_artifact()` produces a JSON-safe dict ready to attach
to a task checkpoint as `success_condition_artifact`.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from claw_v2.verification.success_contract import (
    FileIntegrityCheck,
    StateDeltaSpec,
    SuccessCondition,
)


# ---------------------------------------------------------------------------
# Declarative contracts per tool name. Add new entries here as F3a expands.
# ---------------------------------------------------------------------------


LOCAL_TOOL_SUCCESS_CONDITIONS: dict[str, SuccessCondition] = {
    "Write": SuccessCondition(
        must_contain_keys=("path", "bytes_written"),
        state_delta_check=StateDeltaSpec(
            fs_path="",  # filled at artifact-build time from tool args
            expected_size_delta_bytes=1,
        ),
        forbidden_reasons=("sandbox_block", "outside_workspace", "denied"),
    ),
    # F3a.2 — Edit must demonstrate observable content change. Handlers that
    # legitimately produce a no-op must pass `allow_noop=True` (or
    # `idempotent_ok=True`) in tool_args; the artifact builder will relax
    # `expected_content_changed` for that specific call.
    "Edit": SuccessCondition(
        must_contain_keys=("path", "changed_bytes"),
        state_delta_check=StateDeltaSpec(
            fs_path="",
            expected_size_delta_bytes=0,        # Edit can grow OR shrink
            expected_content_changed=True,      # but content hash MUST change
        ),
        forbidden_reasons=("sandbox_block", "outside_workspace", "denied", "old_text_not_found"),
    ),
    # F3a.2 — Bash exit_code MUST equal 0 for succeeded. The runtime handler
    # may set ok=True with non-zero exit_code (legacy stub); the gate now
    # rejects that contract violation regardless.
    "Bash": SuccessCondition(
        must_contain_keys=("exit_code",),
        must_equal={"exit_code": 0},
        forbidden_reasons=("sandbox_block", "denied", "tool_calls_per_minute breaker"),
    ),
    # F3a.2 — WikiLint default contract = must_be_clean. A clean wiki has
    # issues==[]. To explicitly allow report-only mode (issues=[...] still
    # counts as success), callers can pass `report_only=True` in tool_args;
    # the artifact builder will relax `must_be_empty_keys` for that call.
    "WikiLint": SuccessCondition(
        must_contain_keys=("issues",),
        must_be_empty_keys=("issues",),
        forbidden_reasons=("wiki_unconfigured",),
    ),
    # F3a-extension + F3a-ext.1 — SkillGenerate: re-hash + re-size the
    # generated file and assert it matches the declared values. sha256 MUST
    # be exactly 64 hex.
    "SkillGenerate": SuccessCondition(
        must_contain_keys=("name", "path", "size_bytes", "sha256_hash"),
        must_match_regex={"sha256_hash": r"^[0-9a-fA-F]{64}$"},
        must_be_existing_path=("path",),
        verify_file_integrity=(
            FileIntegrityCheck(
                path_field="path",
                hash_field="sha256_hash",
                size_field="size_bytes",
            ),
        ),
        forbidden_reasons=("not_configured", "invalid_skill_name", "outside_workspace"),
    ),
    # F3a-extension + F3a-ext.1 — AnalyzeImage: text fields must be non-empty
    # (not "", not whitespace-only). No image bytes may land in the artifact.
    "AnalyzeImage": SuccessCondition(
        must_contain_keys=("description", "model_used"),
        must_be_nonempty_str=("description", "model_used"),
        forbidden_reasons=("invalid_image", "timeout", "not_configured"),
    ),
}


# F3a-extension — Bash command_kind sub-contracts. Allows the caller to
# strengthen the base Bash contract for specific command families. The
# artifact builder merges the chosen sub-contract on top of the base when
# `tool_args["command_kind"]` is recognised.
#
# Only git_commit is implemented here. Push/remote operations are NOT in
# F3a scope and remain Tier-3 with no sub-contract.
BASH_COMMAND_KIND_CONTRACTS: dict[str, SuccessCondition] = {
    # F3a-ext.1 — direct semantic validation (no longer relies only on
    # `reason` strings). All four invariants are enforced:
    #   - exit_code == 0
    #   - branch not in {main, master, prod, production}
    #   - before_head != after_head (a commit actually happened)
    #   - after_head == commit_hash (the reported commit IS the new HEAD)
    "git_commit": SuccessCondition(
        must_contain_keys=("exit_code", "commit_hash", "before_head", "after_head", "branch"),
        must_equal={"exit_code": 0},
        must_match_regex={
            "commit_hash": r"^[0-9a-f]{40}$",
            "before_head": r"^[0-9a-f]{40}$",
            "after_head": r"^[0-9a-f]{40}$",
        },
        cross_field_inequality=(("before_head", "after_head"),),
        cross_field_equality=(("after_head", "commit_hash"),),
        forbidden_field_values={
            "branch": ("main", "master", "prod", "production"),
        },
        forbidden_reasons=(
            "sandbox_block",
            "denied",
            "protected_branch_detected",
            "remote_push_attempted",
            "head_unchanged",
        ),
    ),
}


def get_local_tool_success_condition(tool_name: str) -> SuccessCondition | None:
    """Return the declared SuccessCondition for a local tool, or None."""
    return LOCAL_TOOL_SUCCESS_CONDITIONS.get(tool_name)


def resolve_success_condition(tool_name: str) -> SuccessCondition | None:
    """Single contract-registry lookup across LOCAL + EXTERNAL (DV.3 / D10).

    Every runtime consumer (observe_pre_state, state-delta computation,
    attach_artifact_to_result) must resolve contracts through here instead of
    consulting one of the two dicts directly — a LOCAL-only lookup silently
    skipped artifact generation for Tier-3 external tools.
    """
    local = LOCAL_TOOL_SUCCESS_CONDITIONS.get(tool_name)
    if local is not None:
        return local
    try:
        from claw_v2.verification.external_tool_contracts import EXTERNAL_TOOL_SUCCESS_CONDITIONS
        return EXTERNAL_TOOL_SUCCESS_CONDITIONS.get(tool_name)
    except Exception:
        return None


# Backwards-compatible internal alias (pre-D10 name).
_resolve_contract = resolve_success_condition


# ---------------------------------------------------------------------------
# Artifact builder — what the runtime attaches to checkpoint after a Tier-2
# local tool executes. JSON-safe, ready for sqlite persistence.
# ---------------------------------------------------------------------------


def _redacted_args(tool_args: Mapping[str, Any]) -> dict[str, Any]:
    """Privacy-aware projection of tool_args for ledger persistence.

    - Drops content-bearing keys entirely (content, old_text, new_text, image
      bytes/base64, prompts, video/audio bytes, URL-only fields).
    - Replaces credential-like keys with "<redacted>".
    """
    try:
        from claw_v2.verification.external_tool_contracts import EXTERNAL_TOOL_REDACTED_KEYS
        dropped = EXTERNAL_TOOL_REDACTED_KEYS
    except Exception:
        dropped = frozenset({"content", "old_text", "new_text", "image_bytes", "image_b64", "image_data"})
    credential_like = {"token", "secret", "password", "api_key"}
    out: dict[str, Any] = {}
    for k, v in dict(tool_args).items():
        kl = k.lower()
        if k in dropped:
            continue
        if kl in credential_like or "token" in kl or "secret" in kl or "api_key" in kl:
            out[k] = "<redacted>"
            continue
        out[k] = v
    return out


def _serialize_success_condition(sc: SuccessCondition) -> dict[str, Any]:
    return {
        "must_contain_keys": list(sc.must_contain_keys),
        "must_match_regex": dict(sc.must_match_regex),
        # F3b.0 — actually serialize external_check (was wrongly None before).
        "external_check": (
            {
                "kind": sc.external_check.kind,
                "target": sc.external_check.target,
                "expected_phrases": list(sc.external_check.expected_phrases),
                "forbidden_phrases": list(sc.external_check.forbidden_phrases),
                "json_path_equals": sc.external_check.json_path_equals,
                "settle_seconds": sc.external_check.settle_seconds,
                "timeout_seconds": sc.external_check.timeout_seconds,
                "retries": sc.external_check.retries,
            }
            if sc.external_check
            else None
        ),
        "state_delta_check": (
            {
                "db_table": sc.state_delta_check.db_table,
                "expected_rows_delta": sc.state_delta_check.expected_rows_delta,
                "fs_path": sc.state_delta_check.fs_path,
                "expected_size_delta_bytes": sc.state_delta_check.expected_size_delta_bytes,
                "expected_content_changed": sc.state_delta_check.expected_content_changed,
            }
            if sc.state_delta_check
            else None
        ),
        "forbidden_reasons": list(sc.forbidden_reasons),
        "schema_version": sc.schema_version,
        "must_equal": dict(sc.must_equal),
        "must_be_empty_keys": list(sc.must_be_empty_keys),
        "must_be_existing_path": list(sc.must_be_existing_path),
        "must_be_nonempty_str": list(sc.must_be_nonempty_str),
        "cross_field_equality": [list(p) for p in sc.cross_field_equality],
        "cross_field_inequality": [list(p) for p in sc.cross_field_inequality],
        "forbidden_field_values": {k: list(v) for k, v in sc.forbidden_field_values.items()},
        "verify_file_integrity": [
            {
                "path_field": c.path_field,
                "hash_field": c.hash_field,
                "size_field": c.size_field,
                "max_bytes": c.max_bytes,
            }
            for c in sc.verify_file_integrity
        ],
        "allowed_path_roots": list(sc.allowed_path_roots),
    }


def _merge_bash_subcontract(base: SuccessCondition, sub: SuccessCondition) -> SuccessCondition:
    """Merge a Bash command_kind sub-contract on top of the base. Union of
    keys; sub overrides on equal-key conflicts."""
    return SuccessCondition(
        must_contain_keys=tuple(dict.fromkeys((*base.must_contain_keys, *sub.must_contain_keys))),
        must_match_regex={**base.must_match_regex, **sub.must_match_regex},
        external_check=sub.external_check or base.external_check,
        state_delta_check=sub.state_delta_check or base.state_delta_check,
        forbidden_reasons=tuple(dict.fromkeys((*base.forbidden_reasons, *sub.forbidden_reasons))),
        schema_version=base.schema_version,
        must_equal={**base.must_equal, **sub.must_equal},
        must_be_empty_keys=tuple(dict.fromkeys((*base.must_be_empty_keys, *sub.must_be_empty_keys))),
        must_be_existing_path=tuple(dict.fromkeys((*base.must_be_existing_path, *sub.must_be_existing_path))),
        must_be_nonempty_str=tuple(dict.fromkeys((*base.must_be_nonempty_str, *sub.must_be_nonempty_str))),
        cross_field_equality=tuple({*base.cross_field_equality, *sub.cross_field_equality}),
        cross_field_inequality=tuple({*base.cross_field_inequality, *sub.cross_field_inequality}),
        forbidden_field_values={**base.forbidden_field_values, **sub.forbidden_field_values},
        verify_file_integrity=tuple({*base.verify_file_integrity, *sub.verify_file_integrity}),
        allowed_path_roots=tuple(dict.fromkeys((*base.allowed_path_roots, *sub.allowed_path_roots))),
    )


def build_local_tool_artifact(
    *,
    tool_name: str,
    tool_args: Mapping[str, Any],
    tool_result: Mapping[str, Any],
    state_delta_observation: Mapping[str, Any] | None,
    evidence_uri: str | None,
    allowed_path_roots: tuple[str, ...] = (),
    external_observation: Mapping[str, Any] | None = None,
    preflight_passed: bool | None = None,
) -> dict[str, Any]:
    """Build the JSON-safe artifact dict the runtime attaches to a checkpoint.

    Args:
      tool_name: registered tool name (must be in LOCAL_TOOL_SUCCESS_CONDITIONS)
      tool_args: original arguments passed to the tool handler
      tool_result: the dict the handler returned (with ok, path, etc.)
      state_delta_observation: e.g. {"fs_size_added_bytes": 42}
                               or None if no observation pre-fetched.
      evidence_uri: local artifact path (file we wrote, log we captured, etc.)

    Returns a dict shaped exactly as the gate expects in
    `checkpoint["success_condition_artifact"]`.
    """
    base = _resolve_contract(tool_name)
    if base is None:
        raise KeyError(f"No SuccessCondition declared for tool {tool_name!r}")

    # F3a-extension — merge Bash command_kind sub-contract if recognised.
    if tool_name == "Bash":
        kind = str(tool_args.get("command_kind") or "")
        sub = BASH_COMMAND_KIND_CONTRACTS.get(kind)
        if sub is not None:
            base = _merge_bash_subcontract(base, sub)

    # Personalise the state_delta_check.fs_path from the tool args + apply
    # F3a.2 per-call relaxations (allow_noop / idempotent_ok for Edit,
    # report_only for WikiLint).
    sc = base
    allow_noop = bool(tool_args.get("allow_noop") or tool_args.get("idempotent_ok"))
    report_only = bool(tool_args.get("report_only"))
    needs_new_sc = (
        (base.state_delta_check is not None and base.state_delta_check.fs_path == "")
        or (allow_noop and base.state_delta_check is not None and base.state_delta_check.expected_content_changed)
        or (report_only and base.must_be_empty_keys)
    )
    # F3a-ext.1 — inject allowed_path_roots from runtime if the contract has
    # path/integrity checks but no roots declared.
    needs_path_roots = (
        allowed_path_roots
        and not base.allowed_path_roots
        and (base.must_be_existing_path or base.verify_file_integrity)
    )
    if needs_new_sc or needs_path_roots:
        path = str(tool_args.get("path") or "") if base.state_delta_check else ""
        new_sdc = base.state_delta_check
        if new_sdc is not None:
            new_sdc = StateDeltaSpec(
                db_table=new_sdc.db_table,
                expected_rows_delta=new_sdc.expected_rows_delta,
                fs_path=path if new_sdc.fs_path == "" else new_sdc.fs_path,
                expected_size_delta_bytes=new_sdc.expected_size_delta_bytes,
                expected_content_changed=(False if allow_noop else new_sdc.expected_content_changed),
            )
        sc = SuccessCondition(
            must_contain_keys=base.must_contain_keys,
            must_match_regex=base.must_match_regex,
            external_check=base.external_check,
            state_delta_check=new_sdc,
            forbidden_reasons=base.forbidden_reasons,
            schema_version=base.schema_version,
            must_equal=base.must_equal,
            must_be_empty_keys=(() if report_only else base.must_be_empty_keys),
            must_be_existing_path=base.must_be_existing_path,
            must_be_nonempty_str=base.must_be_nonempty_str,
            cross_field_equality=base.cross_field_equality,
            cross_field_inequality=base.cross_field_inequality,
            forbidden_field_values=base.forbidden_field_values,
            verify_file_integrity=base.verify_file_integrity,
            allowed_path_roots=(
                tuple(allowed_path_roots) if needs_path_roots else base.allowed_path_roots
            ),
        )

    # Determine tier: 2 for local, 3 for external. The promote gate enforces
    # additional invariants when tier==3 (preflight + external_check + evidence).
    is_external = False
    try:
        from claw_v2.verification.external_tool_contracts import (
            EXTERNAL_TOOL_PREFLIGHTS,
            EXTERNAL_TOOL_SUCCESS_CONDITIONS,
        )
        is_external = tool_name in EXTERNAL_TOOL_SUCCESS_CONDITIONS
    except Exception:
        EXTERNAL_TOOL_PREFLIGHTS = {}
    preflight_spec = None
    if is_external:
        spec = EXTERNAL_TOOL_PREFLIGHTS.get(tool_name) if isinstance(EXTERNAL_TOOL_PREFLIGHTS, dict) else None
        if spec is not None:
            preflight_spec = {
                "probe_kind": spec.probe_kind,
                "target": spec.target,
                "selectors": list(spec.selectors),
                "must_match": dict(spec.must_match),
                "fail_message": spec.fail_message,
            }

    artifact = {
        "tool_name": tool_name,
        "success_condition": _serialize_success_condition(sc),
        "tool_result": dict(tool_result),
        "tool_args_redacted": _redacted_args(tool_args),
        "external_observation": dict(external_observation) if external_observation else None,
        "state_delta_observation": dict(state_delta_observation) if state_delta_observation else None,
        "evidence_uri": evidence_uri,
        "preflight": preflight_spec,
        "preflight_passed": bool(preflight_passed) if preflight_passed is not None else False,
        "tier": 3 if is_external else 2,
    }
    # Defensive: ensure the artifact survives a json round-trip (audit log)
    json.dumps(artifact)
    return artifact
