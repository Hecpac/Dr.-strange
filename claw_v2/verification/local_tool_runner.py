"""F3a.1 — Runtime auto-generation of `success_condition_artifact` for local
Tier-2 tools that declared a contract.

Hooks into ToolRegistry.execute() to:
  1. Snapshot pre-execution state for any state_delta_check (e.g. filesystem
     size + content hash of the target path).
  2. Call the handler normally.
  3. Snapshot post-execution state.
  4. Build the artifact via build_local_tool_artifact() and attach it under
     the reserved key `_success_condition_artifact` on the result dict.
  5. Set `_contract_required = True` so downstream lift can detect a bypass
     (artifact dropped between runtime and checkpoint).

Privacy / size constraints (per Hector §11):
  - We never persist full file content in the artifact.
  - We hash the post-state content (sha256 first 16 hex) so Edit changes are
    detectable without exposing the bytes.
  - tool_args["content"] is stripped before serialisation (already done in
    build_local_tool_artifact).
"""

from __future__ import annotations

from contextvars import ContextVar
import hashlib
from pathlib import Path
from typing import Any, Mapping

from claw_v2.verification.local_tool_contracts import (
    build_local_tool_artifact,
    resolve_success_condition,
)


# Reserved keys that the runtime writes onto the result dict. Downstream
# consumers (brain / task_handler) lift these into the checkpoint.
ARTIFACT_RESULT_KEY = "_success_condition_artifact"
CONTRACT_REQUIRED_KEY = "_contract_required"
_CURRENT_CONTRACT_TOOL_RESULT: ContextVar[dict[str, Any] | None] = ContextVar(
    "claw_current_contract_tool_result",
    default=None,
)


def _safe_path(args: Mapping[str, Any]) -> Path | None:
    raw = args.get("path") if isinstance(args, Mapping) else None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return Path(raw)
    except (TypeError, ValueError):
        return None


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except (OSError, FileNotFoundError):
        return 0


def _file_hash(path: Path) -> str:
    """sha256 first 16 hex; empty string if file missing/unreadable."""
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except (OSError, FileNotFoundError):
        return ""


def observe_pre_state(tool_name: str, args: Mapping[str, Any]) -> dict[str, Any]:
    """Snapshot before the handler runs. Used to compute state_delta later.

    Returns a dict with `pre_size_bytes` and `pre_content_hash` when the
    contract has an fs_path-based state_delta_check. For tools without an
    fs_path observation (Bash, WikiLint), returns an empty marker dict.
    """
    sc = resolve_success_condition(tool_name)
    if sc is None or sc.state_delta_check is None or sc.state_delta_check.fs_path != "":
        return {"kind": "no_fs_observation"}
    path = _safe_path(args)
    if path is None:
        return {"kind": "no_fs_observation"}
    return {
        "kind": "fs",
        "path": str(path),
        "pre_size_bytes": _file_size(path),
        "pre_content_hash": _file_hash(path),
        "pre_existed": path.exists(),
    }


def compute_state_delta_observation(
    tool_name: str,
    args: Mapping[str, Any],
    pre_state: Mapping[str, Any],
    tool_result: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Compare post-state vs pre-state. Returns the observation dict the
    promote-gate consumes, or None if the contract does not require fs delta."""
    sc = resolve_success_condition(tool_name)
    if sc is None or sc.state_delta_check is None:
        return None
    if sc.state_delta_check.fs_path != "":
        return None  # Reserved for explicitly-set paths; not used today.
    if pre_state.get("kind") != "fs":
        return None
    path = _safe_path(args)
    if path is None:
        return None
    post_size = _file_size(path)
    post_hash = _file_hash(path)
    return {
        "fs_size_added_bytes": max(0, post_size - int(pre_state.get("pre_size_bytes", 0))),
        "post_size_bytes": post_size,
        "post_content_hash": post_hash,
        "content_changed": post_hash != pre_state.get("pre_content_hash"),
        "tool_name": tool_name,
    }


def attach_artifact_to_result(
    *,
    tool_name: str,
    args: Mapping[str, Any],
    result: dict[str, Any],
    pre_state: Mapping[str, Any],
    workspace_root: str | None = None,
) -> dict[str, Any]:
    """In-place add `_success_condition_artifact` to the result dict.

    F3a.2 — fail-closed contract:
      * If the tool has a declared SuccessCondition, the marker
        `_contract_required=True` is set FIRST, idempotently. Even if the
        rest of this function raises, the marker stays so the downstream
        gate detects the bypass and blocks.
      * If the tool has NO declared contract, this is a no-op (legacy tools
        are not affected by the new contract surface).
    """
    # DV.3 / D10 (2026-06-12): one unified registry lookup. The F3b.1 dual
    # membership check (and the LOCAL-only lookups in the state-delta
    # helpers) are replaced by resolve_success_condition.
    if resolve_success_condition(tool_name) is None:
        return result
    if not isinstance(result, dict):
        return result
    # ALWAYS mark first — survives any exception below.
    result[CONTRACT_REQUIRED_KEY] = True
    try:
        state_delta = compute_state_delta_observation(tool_name, args, pre_state, result)
        evidence_uri = None
        # For fs-touching tools, the file itself is local evidence.
        p = _safe_path(args)
        if p is not None and p.exists():
            evidence_uri = str(p)
        # F3b.1 — external tools often carry the artifact path in the RESULT
        # (e.g. HeyGenDeliver's `output_path`), not in args. Fall back to that.
        if evidence_uri is None:
            for field in ("output_path", "evidence_uri", "artifact_path"):
                candidate = result.get(field) if isinstance(result, dict) else None
                if isinstance(candidate, str) and candidate:
                    evidence_uri = candidate
                    break
        # F3a-ext.1 — pass workspace_root so SkillGenerate (et al.) can pin
        # allowed_path_roots and reject artifacts that escape the workspace.
        import tempfile

        roots: tuple[str, ...] = ()
        if workspace_root:
            roots = (str(workspace_root), tempfile.gettempdir())
        # F3b.1 — call optional external_check + preflight providers (mocked
        # in tests, empty in production until F3b.2+ wires real fetchers).
        from claw_v2.verification.external_check_runner import (
            run_external_observation,
            run_preflight,
        )

        external_obs = run_external_observation(tool_name, args, result)
        preflight_passed, preflight_reason = run_preflight(tool_name, args)
        if preflight_reason and preflight_reason != "no_provider":
            result.setdefault("_preflight_reason", preflight_reason)
        artifact = build_local_tool_artifact(
            tool_name=tool_name,
            tool_args=args,
            tool_result=result,
            state_delta_observation=state_delta,
            evidence_uri=evidence_uri,
            allowed_path_roots=roots,
            external_observation=external_obs,
            preflight_passed=preflight_passed if preflight_reason != "no_provider" else None,
        )
        result[ARTIFACT_RESULT_KEY] = artifact
    except Exception as exc:  # noqa: BLE001 — artifact build never breaks the call
        # marker already set; record the error so the gate event is descriptive
        result["_artifact_build_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return result


def remember_tool_contract_result(tool_result: Mapping[str, Any]) -> None:
    """Remember the latest contract-bearing result for the task completion path."""
    if not isinstance(tool_result, Mapping):
        return
    required = bool(tool_result.get(CONTRACT_REQUIRED_KEY))
    artifact_present = ARTIFACT_RESULT_KEY in tool_result
    if not required and not artifact_present:
        return
    liftable: dict[str, Any] = {}
    if required:
        liftable[CONTRACT_REQUIRED_KEY] = True
    if artifact_present:
        liftable[ARTIFACT_RESULT_KEY] = tool_result.get(ARTIFACT_RESULT_KEY)
    _CURRENT_CONTRACT_TOOL_RESULT.set(liftable)


def consume_current_tool_contract_result() -> dict[str, Any] | None:
    """Return and clear the latest contract-bearing result for this context."""
    result = _CURRENT_CONTRACT_TOOL_RESULT.get()
    _CURRENT_CONTRACT_TOOL_RESULT.set(None)
    if not isinstance(result, Mapping):
        return None
    return dict(result)


def reset_current_tool_contract_result() -> None:
    """Clear any contract-bearing result from the current execution context."""
    _CURRENT_CONTRACT_TOOL_RESULT.set(None)


def lift_artifact_to_checkpoint(
    checkpoint: dict[str, Any] | None,
    tool_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Move runtime-attached artifact/marker from result into the checkpoint.

    Returns a NEW checkpoint dict (immutable input semantics for safety).
    If the tool result carries `_contract_required=True` but no artifact
    (e.g. an artifact-build exception), only the marker is propagated —
    the gate will detect the missing artifact and fail closed.
    """
    base = dict(checkpoint or {})
    if not isinstance(tool_result, Mapping):
        return base
    artifact = tool_result.get(ARTIFACT_RESULT_KEY)
    required = bool(tool_result.get(CONTRACT_REQUIRED_KEY))
    if required:
        base["contract_required"] = True
    if artifact is not None:
        base["success_condition_artifact"] = artifact
    return base
