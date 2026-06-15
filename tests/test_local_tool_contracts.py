"""F3a — Local Tier-2 tools declare verifiable success contracts.

Offline. tmp_path filesystem only. NO X/LinkedIn/HeyGen/deploy/GitHub remote.
NO browser/CDP. NO publish. Autouse `_no_network` fixture blocks any accidental
external call.

Demonstrates:
  * Each declared local tool (Write, Edit, Bash, WikiLint) has a SuccessCondition
    pinned in `LOCAL_TOOL_SUCCESS_CONDITIONS`.
  * `build_local_tool_artifact()` produces a JSON-safe artifact attachable to a
    checkpoint.
  * End-to-end through `apply_promote_gate_to_checkpoint()`:
      - local state_delta OK  → terminal_status="succeeded"
      - state_delta missing   → terminal_status="" + verification="pending_verification"
      - tool.ok=True + delta invalid (rows/bytes below threshold) → terminal_status="failed"
"""

from __future__ import annotations

import json
import pytest

from claw_v2.verification.local_tool_contracts import (
    build_local_tool_artifact,
    get_local_tool_success_condition,
)
from claw_v2.verification.promote_gate import apply_promote_gate_to_checkpoint
from claw_v2.verification.success_contract import (
    SuccessCondition,
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("Network call attempted from test_local_tool_contracts — forbidden")

    import socket
    import urllib.request

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    yield


# ---------------------------------------------------------------------------
# 1. Each declared local tool has a contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", ["Write", "Edit", "Bash", "WikiLint"])
def test_local_tool_has_success_condition(tool_name):
    sc = get_local_tool_success_condition(tool_name)
    assert sc is not None, f"{tool_name} missing SuccessCondition"
    assert isinstance(sc, SuccessCondition)
    assert sc.schema_version == "1.0.0"


def test_local_tools_skip_tier3_and_undeclared():
    """We deliberately do NOT declare contracts for WikiDelete (Tier 3 — F3b)
    or GitCommit (no discrete tool def today; goes through Bash)."""
    assert get_local_tool_success_condition("WikiDelete") is None
    assert get_local_tool_success_condition("GitCommit") is None
    assert get_local_tool_success_condition("HeyGenDeliver") is None


# ---------------------------------------------------------------------------
# 2. Registry: declared local tools no longer raise the F1 warn-only contract
#    warning (they HAVE success_condition now). Pure-import smoke test.
# ---------------------------------------------------------------------------


def test_tools_py_attaches_local_contracts():
    """Sanity wire-check: ToolDefinition for Write/Edit/Bash/WikiLint references the contract."""
    import inspect
    from claw_v2 import tools as tools_mod

    src = inspect.getsource(tools_mod)
    assert 'success_condition=LOCAL_TOOL_SUCCESS_CONDITIONS["Write"]' in src
    assert 'success_condition=LOCAL_TOOL_SUCCESS_CONDITIONS["Edit"]' in src
    assert 'success_condition=LOCAL_TOOL_SUCCESS_CONDITIONS["Bash"]' in src
    assert 'success_condition=LOCAL_TOOL_SUCCESS_CONDITIONS["WikiLint"]' in src


# ---------------------------------------------------------------------------
# 3. build_local_tool_artifact: JSON-safe + fs_path personalised from args
# ---------------------------------------------------------------------------


def test_build_artifact_write_personalises_fs_path(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("hello")
    artifact = build_local_tool_artifact(
        tool_name="Write",
        tool_args={"path": str(target), "content": "hello"},
        tool_result={"ok": True, "path": str(target), "bytes_written": 5},
        state_delta_observation={"fs_size_added_bytes": 5},
        evidence_uri=str(target),
    )
    # JSON round-trip survives
    json.dumps(artifact)
    # fs_path bound from args
    assert artifact["success_condition"]["state_delta_check"]["fs_path"] == str(target)
    # Large content NOT persisted in the artifact
    assert "content" not in artifact["tool_args_redacted"]
    # tier and tool_name pinned
    assert artifact["tier"] == 2
    assert artifact["tool_name"] == "Write"


def test_build_artifact_bash_has_no_state_delta_block():
    """Bash legitimately runs without fs delta (e.g. `git status`).
    Its contract must allow that — state_delta_check is None in the artifact."""
    artifact = build_local_tool_artifact(
        tool_name="Bash",
        tool_args={"command": "echo hi"},
        tool_result={"ok": True, "exit_code": 0, "stdout": "hi\n"},
        state_delta_observation=None,
        evidence_uri=None,
    )
    assert artifact["success_condition"]["state_delta_check"] is None


def test_build_artifact_unknown_tool_raises():
    with pytest.raises(KeyError):
        build_local_tool_artifact(
            tool_name="DefinitelyNotARegisteredTool",
            tool_args={},
            tool_result={"ok": True},
            state_delta_observation=None,
            evidence_uri=None,
        )


# ---------------------------------------------------------------------------
# 4. INTEGRATION — realistic checkpoint flows through apply_promote_gate_to_checkpoint
# ---------------------------------------------------------------------------


def _checkpoint_with(artifact: dict) -> dict:
    """Realistic shape of `completed_checkpoint` that task_handler builds."""
    return {
        "verification_status": "passed",
        "actions_taken": ["wrote file"],
        "evidence": [{"kind": "file", "path": artifact.get("evidence_uri")}],
        "success_condition_artifact": artifact,
    }


def test_integration_write_state_delta_ok_yields_succeeded(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("hello world")
    artifact = build_local_tool_artifact(
        tool_name="Write",
        tool_args={"path": str(target), "content": "hello world"},
        tool_result={"ok": True, "path": str(target), "bytes_written": 11},
        state_delta_observation={"fs_size_added_bytes": 11},
        evidence_uri=str(target),
    )
    checkpoint = _checkpoint_with(artifact)
    terminal, verification, new_checkpoint, events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "succeeded"
    assert verification == "passed"
    envelope = new_checkpoint["promote_gate_envelope"]
    assert envelope["status"] == "passed"
    assert envelope["evidence_uri"] == str(target)
    assert events == []  # no degrade event when happy path


def test_integration_write_state_delta_missing_yields_pending(tmp_path):
    target = tmp_path / "missing.txt"
    artifact = build_local_tool_artifact(
        tool_name="Write",
        tool_args={"path": str(target), "content": "x"},
        tool_result={"ok": True, "path": str(target), "bytes_written": 1},
        state_delta_observation=None,  # observation NOT pre-fetched
        evidence_uri=str(target),
    )
    checkpoint = _checkpoint_with(artifact)
    terminal, verification, new_checkpoint, events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == ""  # NOT succeeded
    assert verification == "pending_verification"
    assert new_checkpoint["promote_gate_reason"] == "missing_observation"
    assert any(name == "promote_gate_degraded" for name, _ in events)


def test_integration_write_tool_ok_but_invalid_delta_yields_failed(tmp_path):
    """tool.ok=True but the filesystem grew 0 bytes — must NOT be succeeded."""
    target = tmp_path / "zero.txt"
    target.write_text("")
    artifact = build_local_tool_artifact(
        tool_name="Write",
        tool_args={"path": str(target), "content": "anything"},
        tool_result={"ok": True, "path": str(target), "bytes_written": 8},
        state_delta_observation={
            "fs_size_added_bytes": 0
        },  # invalid: tool claims 8 bytes but FS shows 0
        evidence_uri=str(target),
    )
    checkpoint = _checkpoint_with(artifact)
    terminal, verification, new_checkpoint, events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    assert verification == "failed"
    envelope = new_checkpoint["promote_gate_envelope"]
    assert "state_delta_fs_size_below_threshold" in envelope["verification_result"]["errors"]
    assert any(name == "promote_gate_degraded" for name, _ in events)


def test_integration_edit_missing_required_key_yields_failed(tmp_path):
    """Edit handler returned ok=True but forgot the contract's `changed_bytes` key."""
    target = tmp_path / "code.py"
    target.write_text("x = 1\n")
    artifact = build_local_tool_artifact(
        tool_name="Edit",
        tool_args={"path": str(target), "old_text": "x = 1", "new_text": "x = 2"},
        tool_result={"ok": True, "path": str(target)},  # NOTE: no "changed_bytes"
        state_delta_observation={"fs_size_added_bytes": 0},  # benign for Edit
        evidence_uri=str(target),
    )
    checkpoint = _checkpoint_with(artifact)
    terminal, verification, new_checkpoint, events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    envelope = new_checkpoint["promote_gate_envelope"]
    assert "missing_key:changed_bytes" in envelope["verification_result"]["errors"]


def test_integration_edit_forbidden_reason_old_text_not_found(tmp_path):
    """Edit returned ok=True (defensive handlers sometimes do) but reason
    explains the old_text was not found — must NOT be succeeded."""
    target = tmp_path / "code.py"
    target.write_text("x = 1\n")
    artifact = build_local_tool_artifact(
        tool_name="Edit",
        tool_args={"path": str(target), "old_text": "missing", "new_text": "x"},
        tool_result={
            "ok": True,
            "path": str(target),
            "changed_bytes": 0,
            "reason": "old_text_not_found",
        },
        state_delta_observation={"fs_size_added_bytes": 0},
        evidence_uri=str(target),
    )
    checkpoint = _checkpoint_with(artifact)
    terminal, verification, _new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    assert verification == "failed"


def test_integration_bash_ok_with_exit_code_succeeds():
    """Bash legitimately produces stdout without filesystem delta. The contract
    only demands `exit_code` in result + no forbidden_reason."""
    artifact = build_local_tool_artifact(
        tool_name="Bash",
        tool_args={"command": "pytest tests/ -q"},
        tool_result={"ok": True, "exit_code": 0, "stdout": "40 passed in 0.1s\n"},
        state_delta_observation=None,
        evidence_uri=None,
    )
    checkpoint = _checkpoint_with(artifact)
    terminal, verification, _new_checkpoint, events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "succeeded"
    assert verification == "passed"
    assert events == []


def test_integration_bash_sandbox_block_yields_failed():
    artifact = build_local_tool_artifact(
        tool_name="Bash",
        tool_args={"command": "rm -rf /"},
        tool_result={"ok": True, "exit_code": 0, "stdout": "", "reason": "sandbox_block"},
        state_delta_observation=None,
        evidence_uri=None,
    )
    checkpoint = _checkpoint_with(artifact)
    terminal, verification, new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    assert verification == "failed"
    envelope = new_checkpoint["promote_gate_envelope"]
    assert any(
        "forbidden_reason_matched:sandbox_block" == e
        for e in envelope["verification_result"]["errors"]
    )


def test_integration_wikilint_issues_key_absent_yields_failed():
    """If WikiLint's result lacks the `issues` key entirely, the gate fails.
    (Presence semantics — empty list IS valid; missing key is not.)"""
    artifact = build_local_tool_artifact(
        tool_name="WikiLint",
        tool_args={},
        tool_result={"ok": True},  # NOTE: no "issues" key at all
        state_delta_observation=None,
        evidence_uri=None,
    )
    checkpoint = _checkpoint_with(artifact)
    terminal, _verif, new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    envelope = new_checkpoint["promote_gate_envelope"]
    assert "missing_key:issues" in envelope["verification_result"]["errors"]


def test_integration_wikilint_zero_issues_is_legitimate_success():
    """An empty issues list is a valid 'wiki is clean' result, NOT a failure."""
    artifact = build_local_tool_artifact(
        tool_name="WikiLint",
        tool_args={},
        tool_result={"ok": True, "issues": []},
        state_delta_observation=None,
        evidence_uri=None,
    )
    checkpoint = _checkpoint_with(artifact)
    terminal, verification, _new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "succeeded"
    assert verification == "passed"


def test_integration_wikilint_with_issues_present_now_fails_without_report_only():
    """F3a.2: default WikiLint contract is must_be_clean. Without
    report_only=True, issues=[...] → failed."""
    artifact = build_local_tool_artifact(
        tool_name="WikiLint",
        tool_args={},
        tool_result={"ok": True, "issues": [{"slug": "test", "kind": "stale"}]},
        state_delta_observation=None,
        evidence_uri=None,
    )
    checkpoint = _checkpoint_with(artifact)
    terminal, _verification, _new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"


# ---------------------------------------------------------------------------
# DV.3 / D10 — unified contract registry
# ---------------------------------------------------------------------------


def test_resolve_success_condition_spans_local_and_external() -> None:
    from claw_v2.verification.local_tool_contracts import resolve_success_condition

    assert resolve_success_condition("Write") is not None  # LOCAL
    assert resolve_success_condition("HeyGenDeliver") is not None  # EXTERNAL
    assert resolve_success_condition("NoSuchTool") is None


def test_runner_resolves_contracts_only_through_unified_registry() -> None:
    import inspect

    from claw_v2.verification import local_tool_runner

    src = inspect.getsource(local_tool_runner)
    assert "LOCAL_TOOL_SUCCESS_CONDITIONS" not in src, (
        "local_tool_runner must resolve contracts via resolve_success_condition, "
        "never a single-registry dict (DV.3/D10)"
    )
    assert "EXTERNAL_TOOL_SUCCESS_CONDITIONS" not in src
    assert "resolve_success_condition" in src
