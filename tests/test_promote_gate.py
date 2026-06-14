"""F2.5 — Promote gate tests. Offline. Fake Tier 2 + Tier 3 tools.

Demonstrates that:
  * tool.ok=True cannot produce succeeded if a success_condition exists and
    its verification fails.
  * Tier 2 local can pass to succeeded only when the state_delta_check is
    fully satisfied.
  * Tier 3 lands in pending_verification or blocked if external_check is
    declared without observation, or preflight/evidence are missing.

Also pins the wiring into task_handler.py by importing the integration
target and asserting the gate function is reachable from that module.
NO live calls. NO real X / LinkedIn / HeyGen / deploy / GitHub external.
"""
from __future__ import annotations

import pytest

from claw_v2.verification.success_contract import (
    ExternalCheckSpec,
    PreflightSpec,
    StateDeltaSpec,
    SuccessCondition,
)
from claw_v2.verification.promote_gate import (
    gate_terminal_status,
)


# ---------------------------------------------------------------------------
# Hard guarantee: every test in this file is offline.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("Network call attempted from test_promote_gate — forbidden")

    import socket
    import urllib.request

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    yield


# ---------------------------------------------------------------------------
# Fake Tier 2 tool — "WriteLocalArtifact"
#   - mutates_state=True, tier=2
#   - succeeds only when a row was actually appended to a local SQLite table.
# ---------------------------------------------------------------------------


def _tier2_condition() -> SuccessCondition:
    return SuccessCondition(
        must_contain_keys=("artifact_path", "rows_written"),
        state_delta_check=StateDeltaSpec(
            db_table="agent_tasks",
            expected_rows_delta=1,
        ),
    )


def _tier2_artifact(tool_result, state_delta_observation):
    """Build a JSON-safe artifact the way the runtime would attach to checkpoint."""
    sc = _tier2_condition()
    return {
        "success_condition": {
            "must_contain_keys": list(sc.must_contain_keys),
            "must_match_regex": dict(sc.must_match_regex),
            "external_check": None,
            "state_delta_check": {
                "db_table": sc.state_delta_check.db_table,
                "expected_rows_delta": sc.state_delta_check.expected_rows_delta,
                "fs_path": sc.state_delta_check.fs_path,
                "expected_size_delta_bytes": sc.state_delta_check.expected_size_delta_bytes,
            },
            "forbidden_reasons": list(sc.forbidden_reasons),
            "schema_version": sc.schema_version,
        },
        "tool_result": tool_result,
        "external_observation": None,
        "state_delta_observation": state_delta_observation,
        "evidence_uri": "artifacts/test/tier2_run.json",
        "preflight": None,
        "preflight_passed": False,
        "tier": 2,
    }


def test_tier2_local_succeeds_when_state_delta_passes():
    checkpoint = {
        "verification_status": "passed",
        "success_condition_artifact": _tier2_artifact(
            tool_result={"ok": True, "artifact_path": "out.json", "rows_written": 1},
            state_delta_observation={"rows_added": 1},
        ),
    }
    outcome = gate_terminal_status(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert outcome.terminal_status == "succeeded"
    assert outcome.verification_status == "passed"
    assert outcome.degraded is False
    assert outcome.envelope is not None
    assert outcome.envelope.status == "passed"


def test_tier2_local_blocked_when_state_delta_missing_observation():
    """No observation → pending_verification, NOT succeeded."""
    checkpoint = {
        "verification_status": "passed",
        "success_condition_artifact": _tier2_artifact(
            tool_result={"ok": True, "artifact_path": "out.json", "rows_written": 1},
            state_delta_observation=None,
        ),
    }
    outcome = gate_terminal_status(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert outcome.terminal_status == ""           # NOT succeeded
    assert outcome.verification_status == "pending_verification"
    assert outcome.degraded is True


def test_tier2_local_fails_when_tool_ok_but_state_delta_below_threshold():
    """tool.ok=True, BUT only 0 rows landed — must not be succeeded."""
    checkpoint = {
        "verification_status": "passed",
        "success_condition_artifact": _tier2_artifact(
            tool_result={"ok": True, "artifact_path": "out.json", "rows_written": 1},
            state_delta_observation={"rows_added": 0},
        ),
    }
    outcome = gate_terminal_status(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert outcome.terminal_status == "failed"
    assert outcome.verification_status == "failed"
    assert outcome.degraded is True
    assert outcome.envelope.status == "failed"


# ---------------------------------------------------------------------------
# Fake Tier 3 tool — "PublishExternal"
#   - mutates external state; requires external_check + preflight + evidence
# ---------------------------------------------------------------------------


def _tier3_condition(expected_phrases=("hello", "world")) -> SuccessCondition:
    return SuccessCondition(
        must_contain_keys=("posted_id",),
        external_check=ExternalCheckSpec(
            kind="cdp_phrase",
            target="https://example.test/profile",
            expected_phrases=expected_phrases,
        ),
        forbidden_reasons=("no_editor", "preflight_failed"),
    )


def _tier3_preflight() -> PreflightSpec:
    return PreflightSpec(
        probe_kind="dom_selector_present",
        target="https://example.test/composer",
        selectors=("[data-testid='addButton']",),
        fail_message="add-tweet button missing",
    )


def _tier3_artifact(*, tool_result, external_observation, evidence_uri, preflight_passed, include_preflight=True):
    sc = _tier3_condition()
    pf = _tier3_preflight()
    return {
        "success_condition": {
            "must_contain_keys": list(sc.must_contain_keys),
            "must_match_regex": {},
            "external_check": {
                "kind": sc.external_check.kind,
                "target": sc.external_check.target,
                "expected_phrases": list(sc.external_check.expected_phrases),
                "forbidden_phrases": list(sc.external_check.forbidden_phrases),
                "json_path_equals": None,
                "settle_seconds": sc.external_check.settle_seconds,
                "timeout_seconds": sc.external_check.timeout_seconds,
                "retries": sc.external_check.retries,
            },
            "state_delta_check": None,
            "forbidden_reasons": list(sc.forbidden_reasons),
            "schema_version": sc.schema_version,
        },
        "tool_result": tool_result,
        "external_observation": external_observation,
        "state_delta_observation": None,
        "evidence_uri": evidence_uri,
        "preflight": (
            {
                "probe_kind": pf.probe_kind,
                "target": pf.target,
                "selectors": list(pf.selectors),
                "must_match": {},
                "fail_message": pf.fail_message,
            }
            if include_preflight
            else None
        ),
        "preflight_passed": preflight_passed,
        "tier": 3,
    }


def test_tier3_blocked_when_preflight_missing():
    checkpoint = {
        "verification_status": "passed",
        "success_condition_artifact": _tier3_artifact(
            tool_result={"ok": True, "posted_id": "id1"},
            external_observation={"body_text": "hello world"},
            evidence_uri="artifacts/test/evidence.png",
            preflight_passed=True,
            include_preflight=False,
        ),
    }
    outcome = gate_terminal_status(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert outcome.terminal_status == ""
    assert outcome.verification_status == "blocked"
    assert outcome.degraded is True


def test_tier3_blocked_when_evidence_missing():
    checkpoint = {
        "verification_status": "passed",
        "success_condition_artifact": _tier3_artifact(
            tool_result={"ok": True, "posted_id": "id1"},
            external_observation={"body_text": "hello world"},
            evidence_uri=None,                    # missing
            preflight_passed=True,
        ),
    }
    outcome = gate_terminal_status(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert outcome.terminal_status == ""
    assert outcome.verification_status == "blocked"
    assert outcome.degraded is True


def test_tier3_pending_verification_when_external_observation_missing():
    checkpoint = {
        "verification_status": "passed",
        "success_condition_artifact": _tier3_artifact(
            tool_result={"ok": True, "posted_id": "id1"},
            external_observation=None,            # not pre-fetched yet
            evidence_uri="artifacts/test/evidence.png",
            preflight_passed=True,
        ),
    }
    outcome = gate_terminal_status(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert outcome.terminal_status == ""
    assert outcome.verification_status == "pending_verification"
    assert outcome.degraded is True


def test_tier3_fails_when_external_check_phrases_missing():
    """The X-thread incident, integrated end-to-end through the gate."""
    checkpoint = {
        "verification_status": "passed",
        "success_condition_artifact": _tier3_artifact(
            tool_result={"ok": True, "posted_id": "id1"},
            external_observation={"body_text": "totally unrelated content"},
            evidence_uri="artifacts/test/evidence.png",
            preflight_passed=True,
        ),
    }
    outcome = gate_terminal_status(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert outcome.terminal_status == "failed"
    assert outcome.verification_status == "failed"
    assert outcome.degraded is True
    # Envelope must record exactly which phrases were missing
    errors = outcome.envelope.verification_result["errors"]
    assert any("expected_phrase_missing:hello" == e for e in errors)
    assert any("expected_phrase_missing:world" == e for e in errors)


def test_tier3_succeeds_when_all_conditions_pass():
    checkpoint = {
        "verification_status": "passed",
        "success_condition_artifact": _tier3_artifact(
            tool_result={"ok": True, "posted_id": "id1"},
            external_observation={"body_text": "hello world everyone"},
            evidence_uri="artifacts/test/evidence.png",
            preflight_passed=True,
        ),
    }
    outcome = gate_terminal_status(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert outcome.terminal_status == "succeeded"
    assert outcome.verification_status == "passed"
    assert outcome.degraded is False
    assert outcome.envelope.status == "passed"
    assert outcome.envelope.evidence_uri == "artifacts/test/evidence.png"


# ---------------------------------------------------------------------------
# Backward compatibility: legacy tasks without success_condition_artifact
# must keep their existing behavior. The gate is a pass-through.
# ---------------------------------------------------------------------------


def test_legacy_task_without_artifact_passes_through():
    checkpoint = {"verification_status": "passed"}
    outcome = gate_terminal_status(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert outcome.terminal_status == "succeeded"
    assert outcome.verification_status == "passed"
    assert outcome.degraded is False
    assert outcome.envelope is None


def test_gate_passthrough_when_raw_status_not_succeeded():
    """The gate must never change a non-succeeded outcome."""
    checkpoint = {
        "verification_status": "failed",
        "success_condition_artifact": _tier3_artifact(
            tool_result={"ok": False},
            external_observation=None,
            evidence_uri=None,
            preflight_passed=False,
        ),
    }
    outcome = gate_terminal_status(
        raw_terminal_status="failed",
        raw_verification_status="failed",
        completed_checkpoint=checkpoint,
    )
    assert outcome.terminal_status == "failed"
    assert outcome.verification_status == "failed"
    assert outcome.degraded is False


# ---------------------------------------------------------------------------
# Integration wiring smoke-test: ensure task_handler.py imports the gate
# and the call site is reachable. We do NOT instantiate TaskHandler (heavy);
# we only assert the import path is intact and the symbol exists.
# ---------------------------------------------------------------------------


def test_task_handler_wires_promote_gate():
    """The integration target — task_handler.py — must import the wrapper
    (apply_promote_gate_to_checkpoint). If someone deletes the wire, this fails."""
    import inspect

    from claw_v2 import task_handler
    src = inspect.getsource(task_handler)
    assert "apply_promote_gate_to_checkpoint" in src
    # Ensure the legacy fail-open try/except shim is gone.
    assert "defaulting to legacy behavior" not in src


# ---------------------------------------------------------------------------
# Malformed artifact must NOT silently promote to succeeded.
# ---------------------------------------------------------------------------


def test_gate_artifact_schema_version_mismatch():
    """D8/DV.2 (2026-06-12): an artifact serialized under another schema
    version must park as pending_verification (never silently pass or hard
    fail — the check semantics may have changed)."""
    tool_result = {"ok": True, "artifact_path": "/tmp/x", "rows_written": 1}
    artifact = _tier2_artifact(tool_result, {"db_table": "agent_tasks", "rows_delta": 1})
    artifact["success_condition"]["schema_version"] = "0.9.0"
    outcome = gate_terminal_status(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint={"success_condition_artifact": artifact},
    )
    assert outcome.terminal_status == ""
    assert outcome.verification_status == "pending_verification"
    assert outcome.degraded is True
    assert outcome.reason == "schema_version_mismatch"


def test_gate_regex_guard_rejects_catastrophic_pattern_fast():
    """D9 (2026-06-12): a catastrophic-backtracking pattern over attacker-
    sized input must come back as regex_invalid quickly, not hang the gate."""
    import time as _time

    from claw_v2.verification.success_contract import validate_success_condition

    condition = SuccessCondition(must_match_regex={"output": r"(a+)+$"})
    started = _time.monotonic()
    errors = validate_success_condition(
        tool_result={"ok": True, "output": "a" * 50_000 + "!"},
        condition=condition,
    )
    elapsed = _time.monotonic() - started
    assert "regex_invalid:output" in errors
    assert elapsed < 1.0

    # Valid patterns keep their existing semantics.
    ok_errors = validate_success_condition(
        tool_result={"ok": True, "output": "deploy complete"},
        condition=SuccessCondition(must_match_regex={"output": r"deploy complete"}),
    )
    assert ok_errors == []
    mismatch_errors = validate_success_condition(
        tool_result={"ok": True, "output": "nope"},
        condition=SuccessCondition(must_match_regex={"output": r"deploy complete"}),
    )
    assert mismatch_errors == ["regex_mismatch:output"]
    # An unparseable pattern is reported as invalid, not as a mismatch.
    invalid_errors = validate_success_condition(
        tool_result={"ok": True, "output": "x"},
        condition=SuccessCondition(must_match_regex={"output": r"([unclosed"}),
    )
    assert invalid_errors == ["regex_invalid:output"]


def test_gate_fails_on_malformed_artifact():
    checkpoint = {
        "verification_status": "passed",
        "success_condition_artifact": "this should be a dict, not a string",
    }
    outcome = gate_terminal_status(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert outcome.terminal_status == "failed"
    assert outcome.verification_status == "failed"
    assert outcome.degraded is True
    assert outcome.reason == "malformed_success_condition_artifact"


# ---------------------------------------------------------------------------
# F2.5.1 — fail-closed wrapper.
# Functional tests of apply_promote_gate_to_checkpoint() exercising the path
# task_handler.py actually invokes. NO inspect.getsource; we monkeypatch the
# inner gate_terminal_status to raise and observe the wrapper's behavior.
# ---------------------------------------------------------------------------


from claw_v2.verification import promote_gate as _pg
from claw_v2.verification.promote_gate import apply_promote_gate_to_checkpoint


def test_failclosed_when_gate_raises_and_artifact_present_with_raw_succeeded(monkeypatch):
    """The bypass Hector found: a gate exception MUST NOT leave terminal_status
    at 'succeeded' when a success_condition_artifact was declared."""

    def _boom(**kwargs):
        raise RuntimeError("gate_terminal_status simulated failure")

    monkeypatch.setattr(_pg, "gate_terminal_status", _boom)

    checkpoint = {
        "verification_status": "passed",
        "success_condition_artifact": _tier3_artifact(
            tool_result={"ok": True, "posted_id": "id1"},
            external_observation={"body_text": "hello world"},
            evidence_uri="artifacts/test/evidence.png",
            preflight_passed=True,
        ),
    }

    terminal, verification, new_checkpoint, events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )

    # Critical: must NOT remain 'succeeded'
    assert terminal == ""
    assert verification == "blocked"
    assert new_checkpoint["promote_gate_reason"] == "promote_gate_exception"
    assert new_checkpoint["promote_gate_exception"] == "RuntimeError"
    assert new_checkpoint["verification_status"] == "blocked"
    assert new_checkpoint["promote_gate_envelope"] is None

    # Event emitted with the failed-closed signal
    assert len(events) == 1
    name, payload = events[0]
    assert name == "promote_gate_failed_closed"
    assert payload["exception_type"] == "RuntimeError"
    assert "simulated failure" in payload["exception_message"]
    assert payload["raw_terminal_status"] == "succeeded"


def test_failclosed_legacy_passthrough_when_no_artifact_present(monkeypatch):
    """No artifact → exception cannot affect promotion. Backward-compatible."""

    def _boom(**kwargs):
        raise RuntimeError("gate_terminal_status simulated failure")

    monkeypatch.setattr(_pg, "gate_terminal_status", _boom)

    checkpoint = {"verification_status": "passed"}  # no artifact

    terminal, verification, new_checkpoint, events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )

    # Legacy preserves the existing behavior verbatim
    assert terminal == "succeeded"
    assert verification == "passed"
    assert new_checkpoint == checkpoint
    # An observability event is emitted so the legacy passthrough is auditable
    assert len(events) == 1
    name, payload = events[0]
    assert name == "promote_gate_legacy_passthrough"
    assert payload["reason"] == "no_artifact_present"


def test_failclosed_artifact_malformed_still_downgrades_to_failed():
    """When the artifact is malformed (not a dict), the gate itself returns
    failed — the wrapper carries that through (does NOT promote to succeeded).
    No exception path is taken because the gate handles malformed internally."""
    checkpoint = {
        "verification_status": "passed",
        "success_condition_artifact": "garbage, not a dict",
    }

    terminal, verification, new_checkpoint, events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )

    assert terminal == "failed"
    assert verification == "failed"
    assert new_checkpoint["promote_gate_reason"] == "malformed_success_condition_artifact"
    assert len(events) == 1
    assert events[0][0] == "promote_gate_degraded"


def test_failclosed_passthrough_when_raw_status_was_already_not_succeeded(monkeypatch):
    """If the raw status was never 'succeeded', a gate exception should not
    accidentally upgrade or change it. Wrapper stays passthrough."""

    def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(_pg, "gate_terminal_status", _boom)

    checkpoint = {
        "verification_status": "failed",
        "success_condition_artifact": _tier2_artifact(
            tool_result={"ok": False},
            state_delta_observation=None,
        ),
    }
    terminal, verification, new_checkpoint, events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="failed",
        raw_verification_status="failed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    assert verification == "failed"
    # No fail-closed event because raw was not "succeeded".
    assert all(ev[0] != "promote_gate_failed_closed" for ev in events)


def test_failclosed_happy_path_still_works(monkeypatch):
    """When the gate does NOT raise, the wrapper passes through the gate's
    own decision (no spurious blocked)."""
    # Do not monkeypatch — use the real gate.
    checkpoint = {
        "verification_status": "passed",
        "success_condition_artifact": _tier2_artifact(
            tool_result={"ok": True, "artifact_path": "out.json", "rows_written": 1},
            state_delta_observation={"rows_added": 1},
        ),
    }
    terminal, verification, new_checkpoint, events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "succeeded"
    assert verification == "passed"
    assert new_checkpoint["promote_gate_envelope"] is not None
    assert events == []  # no degrade, no exception, no event
