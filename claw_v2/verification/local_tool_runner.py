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

import contextlib
from contextvars import ContextVar
import hashlib
import threading
from pathlib import Path
from typing import Any, Iterator, Mapping

from claw_v2.verification.local_tool_contracts import (
    build_local_tool_artifact,
    resolve_success_condition,
)


# Reserved keys that the runtime writes onto the result dict. Downstream
# consumers (brain / task_handler) lift these into the checkpoint.
ARTIFACT_RESULT_KEY = "_success_condition_artifact"
CONTRACT_REQUIRED_KEY = "_contract_required"
_ARTIFACT_BUILD_ERROR_KEY = "_artifact_build_error"
_PRE_STATE_ERROR_KEY = "_pre_state_error"
_LIFTABLE_RESULT_KEYS = (
    CONTRACT_REQUIRED_KEY,
    ARTIFACT_RESULT_KEY,
    _ARTIFACT_BUILD_ERROR_KEY,
    _PRE_STATE_ERROR_KEY,
)
_CURRENT_CONTRACT_TOOL_RESULTS: ContextVar[dict[str, Any] | None] = ContextVar(
    "claw_current_contract_tool_results",
    default=None,
)
_CURRENT_CONTRACT_ARTIFACT_SCOPE: ContextVar[str | None] = ContextVar(
    "claw_contract_artifact_scope",
    default=None,
)
_SESSION_CONTRACT_TOOL_RESULTS: dict[str, list[dict[str, Any]]] = {}
_SCOPE_CONTRACT_TOOL_RESULTS: dict[str, list[dict[str, Any]]] = {}
_SESSION_CONTRACT_TOOL_RESULTS_LOCK = threading.Lock()


@contextlib.contextmanager
def contract_artifact_scope(scope_id: str) -> Iterator[None]:
    """Bind contract-artifact propagation to a task/session completion scope."""
    token = _CURRENT_CONTRACT_ARTIFACT_SCOPE.set(scope_id)
    try:
        yield
    finally:
        _CURRENT_CONTRACT_ARTIFACT_SCOPE.reset(token)


def current_contract_artifact_scope() -> str | None:
    """Return the active task-scoped artifact bridge id, if one is bound."""
    return _CURRENT_CONTRACT_ARTIFACT_SCOPE.get()


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
        import os
        import tempfile

        roots: tuple[str, ...] = ()
        if workspace_root:
            roots = (str(workspace_root), tempfile.gettempdir())
        # Review #158 (codex): BrowserScreenshot confines outputs to the
        # browser scratch dir (CLAW_BROWSER_SCRATCH_DIR or ~/.claw/scratch/
        # browser), which is OUTSIDE (workspace_root, tmp). The
        # must_be_existing_path check would otherwise reject a legitimate
        # screenshot as path_outside_allowed_root. Add the scratch dir to the
        # allowed roots when the artifact path lives there.
        if tool_name == "BrowserScreenshot":
            scratch_raw = os.getenv("CLAW_BROWSER_SCRATCH_DIR")
            scratch = (
                Path(scratch_raw).expanduser()
                if scratch_raw
                else Path.home() / ".claw" / "scratch" / "browser"
            ).resolve(strict=False)
            roots = (*roots, str(scratch))
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


def _liftable_contract_result(tool_result: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(tool_result, Mapping):
        return None
    required = bool(tool_result.get(CONTRACT_REQUIRED_KEY))
    artifact_present = ARTIFACT_RESULT_KEY in tool_result
    if not required and not artifact_present:
        return None
    liftable: dict[str, Any] = {}
    for key in _LIFTABLE_RESULT_KEYS:
        if key in tool_result:
            liftable[key] = tool_result.get(key)
    return liftable


def remember_tool_contract_result(
    tool_result: Mapping[str, Any],
    *,
    session_id: str | None = None,
    scope_id: str | None = None,
) -> None:
    """Remember an ordered contract-bearing result for the task completion path."""
    liftable = _liftable_contract_result(tool_result)
    if liftable is None:
        return
    effective_scope_id = scope_id or current_contract_artifact_scope()
    entry = dict(liftable)
    context_payload = _CURRENT_CONTRACT_TOOL_RESULTS.get()
    context_results: list[dict[str, Any]] = []
    if (
        isinstance(context_payload, Mapping)
        and context_payload.get("scope_id") == effective_scope_id
        and context_payload.get("session_id") == session_id
        and isinstance(context_payload.get("results"), list)
    ):
        context_results = [
            dict(item) for item in context_payload["results"] if isinstance(item, Mapping)
        ]
    context_results.append(dict(entry))
    _CURRENT_CONTRACT_TOOL_RESULTS.set(
        {
            "scope_id": effective_scope_id,
            "session_id": session_id,
            "results": context_results,
        }
    )
    if effective_scope_id:
        with _SESSION_CONTRACT_TOOL_RESULTS_LOCK:
            _SCOPE_CONTRACT_TOOL_RESULTS.setdefault(effective_scope_id, []).append(dict(entry))
    elif session_id:
        with _SESSION_CONTRACT_TOOL_RESULTS_LOCK:
            _SESSION_CONTRACT_TOOL_RESULTS.setdefault(session_id, []).append(dict(entry))


def _context_results_for(
    *,
    session_id: str | None = None,
    scope_id: str | None = None,
) -> list[dict[str, Any]]:
    context_payload = _CURRENT_CONTRACT_TOOL_RESULTS.get()
    if not isinstance(context_payload, Mapping):
        return []
    if scope_id is not None and context_payload.get("scope_id") != scope_id:
        return []
    if (
        scope_id is None
        and session_id is not None
        and context_payload.get("session_id") != session_id
    ):
        return []
    results = context_payload.get("results")
    if not isinstance(results, list):
        return []
    return [dict(item) for item in results if isinstance(item, Mapping)]


def current_tool_contract_results(
    *,
    session_id: str | None = None,
    scope_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return remembered contract-bearing results without clearing them."""
    effective_scope_id = scope_id or current_contract_artifact_scope()
    collected: list[dict[str, Any]] = []
    if effective_scope_id:
        with _SESSION_CONTRACT_TOOL_RESULTS_LOCK:
            results = _SCOPE_CONTRACT_TOOL_RESULTS.get(effective_scope_id, [])
        collected.extend(dict(item) for item in results if isinstance(item, Mapping))
    if session_id:
        with _SESSION_CONTRACT_TOOL_RESULTS_LOCK:
            results = _SESSION_CONTRACT_TOOL_RESULTS.get(session_id, [])
        collected.extend(dict(item) for item in results if isinstance(item, Mapping))
    if collected:
        return collected
    return _context_results_for(session_id=session_id, scope_id=effective_scope_id)


def current_tool_contract_result(
    *,
    session_id: str | None = None,
    scope_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the latest contract-bearing result without clearing it."""
    results = current_tool_contract_results(session_id=session_id, scope_id=scope_id)
    return dict(results[-1]) if results else None


def _clear_context_results_for(
    *,
    session_id: str | None = None,
    scope_id: str | None = None,
) -> None:
    context_payload = _CURRENT_CONTRACT_TOOL_RESULTS.get()
    if not isinstance(context_payload, Mapping):
        return
    if scope_id is None and session_id is None:
        _CURRENT_CONTRACT_TOOL_RESULTS.set(None)
        return
    if scope_id is not None and context_payload.get("scope_id") == scope_id:
        _CURRENT_CONTRACT_TOOL_RESULTS.set(None)
        return
    if (
        scope_id is None
        and session_id is not None
        and context_payload.get("session_id") == session_id
    ):
        _CURRENT_CONTRACT_TOOL_RESULTS.set(None)


def consume_current_tool_contract_results(
    *,
    session_id: str | None = None,
    scope_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return and clear contract-bearing results for this task/session scope."""
    effective_scope_id = scope_id or current_contract_artifact_scope()
    collected: list[dict[str, Any]] = []
    if effective_scope_id:
        with _SESSION_CONTRACT_TOOL_RESULTS_LOCK:
            scope_results = _SCOPE_CONTRACT_TOOL_RESULTS.pop(effective_scope_id, [])
        collected.extend(dict(item) for item in scope_results if isinstance(item, Mapping))
        _clear_context_results_for(scope_id=effective_scope_id)
    if session_id:
        with _SESSION_CONTRACT_TOOL_RESULTS_LOCK:
            session_results = _SESSION_CONTRACT_TOOL_RESULTS.pop(session_id, [])
        collected.extend(dict(item) for item in session_results if isinstance(item, Mapping))
        _clear_context_results_for(session_id=session_id)
    if collected:
        return collected
    context_results = _context_results_for(session_id=session_id, scope_id=effective_scope_id)
    _clear_context_results_for(session_id=session_id, scope_id=effective_scope_id)
    return context_results


def consume_current_tool_contract_result(
    *,
    session_id: str | None = None,
    scope_id: str | None = None,
) -> dict[str, Any] | None:
    """Return and clear the latest contract-bearing result for this context."""
    results = consume_current_tool_contract_results(session_id=session_id, scope_id=scope_id)
    return dict(results[-1]) if results else None


def reset_current_tool_contract_results(
    *,
    session_id: str | None = None,
    scope_id: str | None = None,
) -> None:
    """Clear contract-bearing results for a task/session scope."""
    effective_scope_id = scope_id or current_contract_artifact_scope()
    if effective_scope_id:
        with _SESSION_CONTRACT_TOOL_RESULTS_LOCK:
            _SCOPE_CONTRACT_TOOL_RESULTS.pop(effective_scope_id, None)
    if session_id:
        with _SESSION_CONTRACT_TOOL_RESULTS_LOCK:
            _SESSION_CONTRACT_TOOL_RESULTS.pop(session_id, None)
    _clear_context_results_for(session_id=session_id, scope_id=effective_scope_id)


def reset_current_tool_contract_result(
    *,
    session_id: str | None = None,
    scope_id: str | None = None,
) -> None:
    """Clear any contract-bearing result from the current execution context."""
    reset_current_tool_contract_results(session_id=session_id, scope_id=scope_id)


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
    return lift_artifacts_to_checkpoint(checkpoint, [tool_result])


def lift_artifacts_to_checkpoint(
    checkpoint: dict[str, Any] | None,
    tool_results: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    """Move runtime-attached artifact markers from multiple results into a checkpoint."""
    base = dict(checkpoint or {})
    artifacts: list[Any] = []
    existing_artifacts = base.get("success_condition_artifacts")
    if isinstance(existing_artifacts, list):
        artifacts.extend(existing_artifacts)
    else:
        existing_artifact = base.get("success_condition_artifact")
        if existing_artifact is not None:
            artifacts.append(existing_artifact)
    required = bool(base.get("contract_required"))
    for tool_result in tool_results:
        if not isinstance(tool_result, Mapping):
            continue
        if bool(tool_result.get(CONTRACT_REQUIRED_KEY)):
            required = True
        artifact = tool_result.get(ARTIFACT_RESULT_KEY)
        if artifact is not None:
            artifacts.append(artifact)
    if required:
        base["contract_required"] = True
    if artifacts:
        base["success_condition_artifacts"] = list(artifacts)
        base["success_condition_artifact"] = artifacts[0]
    elif required:
        base["success_condition_artifacts"] = []
    return base
