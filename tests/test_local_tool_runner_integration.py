"""F3a.1 — End-to-end functional tests.

These tests DO NOT build the checkpoint by hand. They exercise the real
flow that the runtime takes:

    ToolDefinition.success_condition declared
        → ToolRegistry.execute() invokes handler
        → local_tool_runner.attach_artifact_to_result() injects artifact
        → lift_artifact_to_checkpoint() moves it onto the checkpoint
        → apply_promote_gate_to_checkpoint() decides terminal_status

100% offline. tmp_path filesystem only. Autouse `_no_network` fixture blocks
sockets/urllib. NO X/LinkedIn/HeyGen/deploy/GitHub remote, no browser/CDP.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from claw_v2.tools import ToolDefinition, ToolRegistry
from claw_v2.verification.local_tool_contracts import LOCAL_TOOL_SUCCESS_CONDITIONS
from claw_v2.verification.local_tool_runner import (
    ARTIFACT_RESULT_KEY,
    CONTRACT_REQUIRED_KEY,
    consume_current_tool_contract_result,
    current_tool_contract_result,
    lift_artifact_to_checkpoint,
    remember_tool_contract_result,
    reset_current_tool_contract_result,
)
from claw_v2.verification.promote_gate import apply_promote_gate_to_checkpoint


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("Network call attempted from runner integration test — forbidden")

    import socket
    import urllib.request

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    yield


# ---------------------------------------------------------------------------
# Fake handlers — pure-Python, no real I/O outside tmp_path.
# ---------------------------------------------------------------------------


def _fake_write_handler_factory(target_path: str, payload: str):
    """Returns a handler that writes payload to target_path and reports ok=True."""

    def _handler(args):
        # Tool args are the dict the registry forwards from the caller.
        from pathlib import Path

        Path(args["path"]).write_text(args["content"])
        return {"ok": True, "path": args["path"], "bytes_written": len(args["content"])}

    return _handler


def _fake_write_handler_says_ok_but_writes_nothing(args):
    """Lying handler: claims ok=True without touching the filesystem."""
    return {"ok": True, "path": args["path"], "bytes_written": len(args["content"])}


def _fake_bash_handler(args):
    """Honest bash mock — returns exit_code from args or 0."""
    cmd = str(args.get("command", ""))
    code = int(args.get("_fake_exit_code", 0))
    return {"ok": code == 0, "exit_code": code, "stdout": f"ran: {cmd}\n"}


def _fake_edit_handler_factory(target_path: str, new_content: str):
    def _handler(args):
        from pathlib import Path

        p = Path(args["path"])
        old = p.read_text() if p.exists() else ""
        old_text = args.get("old_text", "")
        if old_text and old_text not in old:
            return {
                "ok": True,
                "path": args["path"],
                "changed_bytes": 0,
                "reason": "old_text_not_found",
            }
        p.write_text(new_content)
        return {"ok": True, "path": args["path"], "changed_bytes": abs(len(new_content) - len(old))}

    return _handler


def _fake_wikilint_handler_factory(issues_list):
    def _handler(args):
        return {"ok": True, "issues": list(issues_list)}

    return _handler


# ---------------------------------------------------------------------------
# Helper: build a minimal ToolRegistry with one fake tool registered against
# the real SuccessCondition for that name. Bypasses sandbox/network/approval
# (none required for these local Tier-2 contracts).
# ---------------------------------------------------------------------------


def _registry_with(tool_name: str, handler, *, workspace_root=None):
    from pathlib import Path

    reg = ToolRegistry(workspace_root=Path(workspace_root) if workspace_root else Path("/tmp"))
    sc = LOCAL_TOOL_SUCCESS_CONDITIONS[tool_name]
    reg.register(
        ToolDefinition(
            name=tool_name,
            description=f"fake {tool_name} for tests",
            allowed_agent_classes=("operator",),
            handler=handler,
            mutates_state=True,
            tier=2,
            success_condition=sc,
        )
    )
    return reg


def test_contract_result_cross_thread_session_store_is_one_shot():
    session_id = "session-A"
    reset_current_tool_contract_result(session_id=session_id)
    payload = {
        CONTRACT_REQUIRED_KEY: True,
        ARTIFACT_RESULT_KEY: {"tool_name": "Write"},
        "_artifact_build_error": "RuntimeError: simulated",
        "full_output": "this must not be stored at top level",
    }

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(remember_tool_contract_result, payload, session_id=session_id).result()

    stored = consume_current_tool_contract_result(session_id=session_id)

    assert stored is not None
    assert stored[CONTRACT_REQUIRED_KEY] is True
    assert stored[ARTIFACT_RESULT_KEY] == {"tool_name": "Write"}
    assert stored["_artifact_build_error"] == "RuntimeError: simulated"
    assert "full_output" not in stored
    assert consume_current_tool_contract_result(session_id=session_id) is None


def test_contract_result_session_isolation_and_reset_are_session_scoped():
    session_a = "session-A"
    session_b = "session-B"
    reset_current_tool_contract_result(session_id=session_a)
    reset_current_tool_contract_result(session_id=session_b)
    payload_a = {CONTRACT_REQUIRED_KEY: True, ARTIFACT_RESULT_KEY: {"tool_name": "Write"}}
    payload_b = {CONTRACT_REQUIRED_KEY: True, ARTIFACT_RESULT_KEY: {"tool_name": "Bash"}}

    with ThreadPoolExecutor(max_workers=2) as pool:
        pool.submit(remember_tool_contract_result, payload_a, session_id=session_a).result()
        pool.submit(remember_tool_contract_result, payload_b, session_id=session_b).result()

    assert consume_current_tool_contract_result(session_id=session_a) == payload_a
    assert current_tool_contract_result(session_id=session_b) == payload_b

    reset_current_tool_contract_result(session_id=session_a)

    assert current_tool_contract_result(session_id=session_b) == payload_b
    assert consume_current_tool_contract_result(session_id=session_b) == payload_b
    assert consume_current_tool_contract_result(session_id=session_b) is None


def test_contract_result_context_fallback_still_works_without_session_id():
    reset_current_tool_contract_result()
    payload = {CONTRACT_REQUIRED_KEY: True, ARTIFACT_RESULT_KEY: {"tool_name": "WikiLint"}}

    remember_tool_contract_result(payload)

    assert current_tool_contract_result() == payload
    assert consume_current_tool_contract_result() == payload
    assert consume_current_tool_contract_result() is None


def test_tool_registry_execute_stores_minimal_contract_result_by_session(tmp_path):
    session_id = "session-A"
    reset_current_tool_contract_result(session_id=session_id)
    target = tmp_path / "out.txt"
    payload = "secret payload must not be copied outside the artifact"
    reg = _registry_with(
        "Write",
        _fake_write_handler_factory(str(target), payload),
        workspace_root=tmp_path,
    )

    result = reg.execute(
        "Write",
        {"path": str(target), "content": payload},
        agent_class="operator",
        session_id=session_id,
    )
    stored = consume_current_tool_contract_result(session_id=session_id)

    assert result[CONTRACT_REQUIRED_KEY] is True
    assert ARTIFACT_RESULT_KEY in result
    assert stored is not None
    assert set(stored).issubset(
        {
            CONTRACT_REQUIRED_KEY,
            ARTIFACT_RESULT_KEY,
            "_artifact_build_error",
            "_pre_state_error",
        }
    )
    assert stored[CONTRACT_REQUIRED_KEY] is True
    assert stored[ARTIFACT_RESULT_KEY]["tool_name"] == "Write"
    assert payload not in str(stored)


# ---------------------------------------------------------------------------
# WRITE — full integration
# ---------------------------------------------------------------------------


def test_runtime_write_valid_run_attaches_artifact_and_succeeds(tmp_path):
    target = tmp_path / "out.txt"
    payload = "hello world"
    reg = _registry_with("Write", _fake_write_handler_factory(str(target), payload))
    result = reg.execute(
        "Write",
        {"path": str(target), "content": payload},
        agent_class="operator",
    )
    # 1. Runtime attached the artifact
    assert ARTIFACT_RESULT_KEY in result
    assert result[CONTRACT_REQUIRED_KEY] is True
    artifact = result[ARTIFACT_RESULT_KEY]
    # 2. Artifact references the right tool + has state_delta observation
    assert artifact["tool_name"] == "Write"
    assert artifact["state_delta_observation"]["fs_size_added_bytes"] == len(payload)
    assert artifact["state_delta_observation"]["content_changed"] is True
    # 3. Lift into checkpoint and run the gate
    checkpoint = lift_artifact_to_checkpoint(
        {"verification_status": "passed"},
        result,
    )
    terminal, verification, _new_checkpoint, events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "succeeded"
    assert verification == "passed"
    assert events == []


def test_runtime_write_lying_handler_with_invalid_delta_fails(tmp_path):
    """Handler returns ok=True with bytes_written>0 but did NOT touch the FS.
    The runtime's pre/post snapshot sees fs_size_added_bytes=0 → gate fails."""
    target = tmp_path / "fake.txt"
    reg = _registry_with("Write", _fake_write_handler_says_ok_but_writes_nothing)
    result = reg.execute(
        "Write",
        {"path": str(target), "content": "claims-50-bytes-but-writes-nothing"},
        agent_class="operator",
    )
    assert ARTIFACT_RESULT_KEY in result
    artifact = result[ARTIFACT_RESULT_KEY]
    assert artifact["state_delta_observation"]["fs_size_added_bytes"] == 0
    checkpoint = lift_artifact_to_checkpoint(
        {"verification_status": "passed"},
        result,
    )
    terminal, verification, new_checkpoint, events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    assert verification == "failed"
    envelope = new_checkpoint["promote_gate_envelope"]
    assert "state_delta_fs_size_below_threshold" in envelope["verification_result"]["errors"]


def test_runtime_write_artifact_missing_from_checkpoint_blocks(tmp_path):
    """A simulated downstream bug: the runtime attached the artifact, but the
    code that builds the checkpoint dropped it. We still record
    `contract_required=True` so the gate blocks instead of silently passing."""
    target = tmp_path / "out.txt"
    payload = "hello"
    reg = _registry_with("Write", _fake_write_handler_factory(str(target), payload))
    result = reg.execute(
        "Write",
        {"path": str(target), "content": payload},
        agent_class="operator",
    )
    assert result[CONTRACT_REQUIRED_KEY] is True
    assert ARTIFACT_RESULT_KEY in result
    # Simulate downstream lift bug: keep the marker, drop the artifact.
    broken_checkpoint = {
        "verification_status": "passed",
        "contract_required": True,
        # success_condition_artifact intentionally NOT set
    }
    terminal, verification, new_checkpoint, events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=broken_checkpoint,
    )
    # Critical: NOT succeeded; bypass detected
    assert terminal == ""
    assert verification == "blocked"
    assert new_checkpoint["promote_gate_reason"] == "contract_required_artifact_missing"
    assert any(name == "promote_gate_contract_bypass_detected" for name, _ in events)


# ---------------------------------------------------------------------------
# BASH — exit_code semantics
# ---------------------------------------------------------------------------


def test_runtime_bash_exit_code_zero_succeeds():
    reg = _registry_with("Bash", _fake_bash_handler)
    result = reg.execute(
        "Bash",
        {"command": "pytest -q", "_fake_exit_code": 0},
        agent_class="operator",
    )
    assert ARTIFACT_RESULT_KEY in result
    checkpoint = lift_artifact_to_checkpoint(
        {"verification_status": "passed"},
        result,
    )
    terminal, verification, _new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "succeeded"
    assert verification == "passed"


def test_runtime_bash_exit_code_nonzero_fails():
    """exit_code=1 → handler returns ok=False → success_condition does not need
    to evaluate; tool_not_ok already blocks."""
    reg = _registry_with("Bash", _fake_bash_handler)
    result = reg.execute(
        "Bash",
        {"command": "pytest -q", "_fake_exit_code": 1},
        agent_class="operator",
    )
    # Even though exit_code key is present, ok=False means contract evaluates to failed.
    checkpoint = lift_artifact_to_checkpoint(
        {"verification_status": "passed"},
        result,
    )
    terminal, verification, new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    assert verification == "failed"
    envelope = new_checkpoint["promote_gate_envelope"]
    assert "tool_not_ok" in envelope["verification_result"]["errors"]


# ---------------------------------------------------------------------------
# WIKILINT — issues semantics
# ---------------------------------------------------------------------------


def test_runtime_wikilint_zero_issues_succeeds():
    reg = _registry_with("WikiLint", _fake_wikilint_handler_factory([]))
    result = reg.execute("WikiLint", {}, agent_class="operator")
    assert ARTIFACT_RESULT_KEY in result
    checkpoint = lift_artifact_to_checkpoint(
        {"verification_status": "passed"},
        result,
    )
    terminal, verification, _new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "succeeded"
    assert verification == "passed"


def test_runtime_wikilint_default_strict_rejects_issues_when_no_report_only():
    """F3a.2 supersedes the F3a.1 looser behavior: the default WikiLint
    contract is must_be_clean. issues=[...] without report_only=True → failed."""
    issues = [{"slug": "stale1", "kind": "stale"}, {"slug": "broken1", "kind": "broken_link"}]
    reg = _registry_with("WikiLint", _fake_wikilint_handler_factory(issues))
    result = reg.execute("WikiLint", {}, agent_class="operator")
    checkpoint = lift_artifact_to_checkpoint(
        {"verification_status": "passed"},
        result,
    )
    terminal, _verification, _new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"


# ---------------------------------------------------------------------------
# EDIT — hash-based change detection (no full content in ledger)
# ---------------------------------------------------------------------------


def test_runtime_edit_with_real_change_succeeds(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("x = 1\n")
    reg = _registry_with("Edit", _fake_edit_handler_factory(str(target), "x = 2\n"))
    result = reg.execute(
        "Edit",
        {"path": str(target), "old_text": "x = 1", "new_text": "x = 2"},
        agent_class="operator",
    )
    artifact = result[ARTIFACT_RESULT_KEY]
    obs = artifact["state_delta_observation"]
    assert obs["content_changed"] is True
    # Pre and post hashes BOTH present, but file content NOT in artifact
    assert "pre_content_hash" not in obs  # only post-hash is in observation
    assert "post_content_hash" in obs
    # The content itself is NOT in the artifact (privacy §11)
    artifact_str = str(artifact)
    assert "x = 1" not in artifact_str
    assert "x = 2" not in artifact_str

    checkpoint = lift_artifact_to_checkpoint(
        {"verification_status": "passed"},
        result,
    )
    terminal, verification, _new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "succeeded"
    assert verification == "passed"


def test_runtime_edit_no_change_without_allow_noop_now_fails(tmp_path):
    """F3a.2 supersedes the F3a.1 looser behavior: Edit content_unchanged
    without allow_noop=True → failed."""
    target = tmp_path / "code.py"
    target.write_text("abc")

    def _no_op_handler(args):
        return {"ok": True, "path": args["path"], "changed_bytes": 0}

    reg = _registry_with("Edit", _no_op_handler)
    result = reg.execute(
        "Edit",
        {"path": str(target), "old_text": "abc", "new_text": "abc"},
        agent_class="operator",
    )
    checkpoint = lift_artifact_to_checkpoint(
        {"verification_status": "passed"},
        result,
    )
    terminal, _verification, _new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"


def test_runtime_edit_handler_reports_old_text_not_found_fails(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("x = 1\n")
    reg = _registry_with("Edit", _fake_edit_handler_factory(str(target), "x = 2\n"))
    result = reg.execute(
        "Edit",
        {"path": str(target), "old_text": "DOES NOT EXIST", "new_text": "x = 2"},
        agent_class="operator",
    )
    checkpoint = lift_artifact_to_checkpoint(
        {"verification_status": "passed"},
        result,
    )
    terminal, _v, new_checkpoint, _e = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    envelope = new_checkpoint["promote_gate_envelope"]
    assert any(
        "forbidden_reason_matched:old_text_not_found" == e
        for e in envelope["verification_result"]["errors"]
    )


# ---------------------------------------------------------------------------
# Privacy / ledger size (§11)
# ---------------------------------------------------------------------------


# ===========================================================================
# F3a.2 — fail-closed when attach_artifact_to_result OR observe_pre_state
# raises. The result MUST carry contract_required=True regardless, and the
# downstream gate MUST block. No legacy silent passthrough.
# ===========================================================================


def test_failclosed_when_attach_artifact_raises(monkeypatch, tmp_path):
    """Bypass test (§B): if attach_artifact_to_result blows up, the runtime
    must still mark contract_required and the gate must block succeed."""
    from claw_v2.verification import local_tool_runner as ltr

    def _boom(*a, **kw):
        raise RuntimeError("attach_artifact_to_result simulated failure")

    monkeypatch.setattr(ltr, "attach_artifact_to_result", _boom)

    target = tmp_path / "out.txt"
    payload = "hello"
    reg = _registry_with("Write", _fake_write_handler_factory(str(target), payload))
    result = reg.execute(
        "Write",
        {"path": str(target), "content": payload},
        agent_class="operator",
    )
    # Marker survived the exception
    assert result.get("_contract_required") is True
    # Artifact NOT attached
    assert ARTIFACT_RESULT_KEY not in result
    # Structured error surfaced
    assert "_artifact_build_error" in result

    checkpoint = lift_artifact_to_checkpoint(
        {"verification_status": "passed"},
        result,
    )
    terminal, verification, new_checkpoint, events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == ""  # NOT succeeded
    assert verification == "blocked"
    assert new_checkpoint["promote_gate_reason"] == "contract_required_artifact_missing"
    assert any(name == "promote_gate_contract_bypass_detected" for name, _ in events)


def test_failclosed_when_observe_pre_state_raises(monkeypatch, tmp_path):
    """Bypass test (§C): if observe_pre_state blows up before the handler
    runs, the runtime must still mark contract_required and the gate must
    block succeed."""
    from claw_v2.verification import local_tool_runner as ltr

    def _boom(*a, **kw):
        raise RuntimeError("observe_pre_state simulated failure")

    monkeypatch.setattr(ltr, "observe_pre_state", _boom)

    target = tmp_path / "out.txt"
    reg = _registry_with("Write", _fake_write_handler_factory(str(target), "abc"))
    result = reg.execute(
        "Write",
        {"path": str(target), "content": "abc"},
        agent_class="operator",
    )
    # Marker survived pre-state failure
    assert result.get("_contract_required") is True
    # Either no artifact OR an artifact still built from empty pre_state.
    # The critical assertion: nothing silently promotes to succeeded.
    checkpoint = lift_artifact_to_checkpoint(
        {"verification_status": "passed"},
        result,
    )
    terminal, verification, _new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    # With pre_state empty, state_delta observation is None → pending_verification
    # OR the gate blocks via missing artifact. Either way: NOT succeeded.
    assert terminal != "succeeded"


# ===========================================================================
# F3a.2 — Bash exit_code must equal 0 (must_equal contract enforcement).
# ===========================================================================


def test_bash_ok_true_but_exit_code_nonzero_fails():
    """Per §D: if a handler returns ok=True with non-zero exit_code, the
    must_equal={'exit_code': 0} contract must reject it."""

    def _lying_bash(args):
        # Defensive handler — claims ok=True even though exit_code says failure.
        return {"ok": True, "exit_code": int(args.get("_fake_exit_code", 1)), "stdout": "weird"}

    reg = _registry_with("Bash", _lying_bash)
    result = reg.execute("Bash", {"command": "false", "_fake_exit_code": 1}, agent_class="operator")
    checkpoint = lift_artifact_to_checkpoint(
        {"verification_status": "passed"},
        result,
    )
    terminal, verification, new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    assert verification == "failed"
    envelope = new_checkpoint["promote_gate_envelope"]
    assert "must_equal_mismatch:exit_code" in envelope["verification_result"]["errors"]


# ===========================================================================
# F3a.2 — WikiLint must_be_clean default + report_only opt-in.
# ===========================================================================


def test_wikilint_default_must_be_clean_passes_when_empty():
    reg = _registry_with("WikiLint", _fake_wikilint_handler_factory([]))
    result = reg.execute("WikiLint", {}, agent_class="operator")
    checkpoint = lift_artifact_to_checkpoint(
        {"verification_status": "passed"},
        result,
    )
    terminal, verification, _new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "succeeded"
    assert verification == "passed"


def test_wikilint_default_must_be_clean_fails_when_issues_present():
    issues = [{"slug": "stale1", "kind": "stale"}]
    reg = _registry_with("WikiLint", _fake_wikilint_handler_factory(issues))
    result = reg.execute("WikiLint", {}, agent_class="operator")
    checkpoint = lift_artifact_to_checkpoint(
        {"verification_status": "passed"},
        result,
    )
    terminal, verification, new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    assert verification == "failed"
    envelope = new_checkpoint["promote_gate_envelope"]
    assert "must_be_empty_violated:issues" in envelope["verification_result"]["errors"]


def test_wikilint_report_only_opt_in_succeeds_with_issues():
    """Explicit `report_only=True` in tool_args relaxes must_be_empty."""
    issues = [{"slug": "stale1", "kind": "stale"}, {"slug": "broken1", "kind": "broken_link"}]
    reg = _registry_with("WikiLint", _fake_wikilint_handler_factory(issues))
    result = reg.execute(
        "WikiLint",
        {"report_only": True},
        agent_class="operator",
    )
    checkpoint = lift_artifact_to_checkpoint(
        {"verification_status": "passed"},
        result,
    )
    terminal, verification, _new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "succeeded"
    assert verification == "passed"


# ===========================================================================
# F3a.2 — Edit must show observable content change by default. allow_noop /
# idempotent_ok in tool_args relaxes that requirement.
# ===========================================================================


def test_edit_no_op_fails_by_default(tmp_path):
    """Per §F: handler returns ok=True but file content did NOT change.
    Without allow_noop, the contract must fail."""
    target = tmp_path / "code.py"
    target.write_text("x = 1\n")

    def _no_op_handler(args):
        # Don't touch the file; report zero changes honestly.
        return {"ok": True, "path": args["path"], "changed_bytes": 0}

    reg = _registry_with("Edit", _no_op_handler)
    result = reg.execute(
        "Edit",
        {"path": str(target), "old_text": "x = 1", "new_text": "x = 1"},
        agent_class="operator",
    )
    checkpoint = lift_artifact_to_checkpoint(
        {"verification_status": "passed"},
        result,
    )
    terminal, verification, new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    assert verification == "failed"
    envelope = new_checkpoint["promote_gate_envelope"]
    assert "state_delta_content_unchanged" in envelope["verification_result"]["errors"]


def test_edit_allow_noop_succeeds_when_state_is_already_correct(tmp_path):
    """idempotent_ok / allow_noop flag relaxes content_changed requirement
    when the handler signals the file is already in the desired state."""
    target = tmp_path / "code.py"
    target.write_text("x = 2\n")  # already in target state

    def _idempotent_handler(args):
        return {"ok": True, "path": args["path"], "changed_bytes": 0}

    reg = _registry_with("Edit", _idempotent_handler)
    result = reg.execute(
        "Edit",
        {"path": str(target), "old_text": "x = 2", "new_text": "x = 2", "allow_noop": True},
        agent_class="operator",
    )
    checkpoint = lift_artifact_to_checkpoint(
        {"verification_status": "passed"},
        result,
    )
    terminal, verification, _new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "succeeded"
    assert verification == "passed"


def test_artifact_never_contains_file_content(tmp_path):
    """The artifact persisted in the checkpoint MUST NOT carry tool_args['content']."""
    target = tmp_path / "secret.txt"
    secret = "SUPER-SECRET-CONTENT-THAT-MUST-NOT-LEAK-INTO-THE-LEDGER"
    reg = _registry_with("Write", _fake_write_handler_factory(str(target), secret))
    result = reg.execute(
        "Write",
        {"path": str(target), "content": secret},
        agent_class="operator",
    )
    artifact = result[ARTIFACT_RESULT_KEY]
    artifact_str = str(artifact)
    assert secret not in artifact_str
    # tool_args_redacted should not contain "content" key
    assert "content" not in artifact["tool_args_redacted"]
