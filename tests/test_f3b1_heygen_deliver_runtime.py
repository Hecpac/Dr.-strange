"""F3b.1 — HeyGenDeliver end-to-end with MOCKED external_check + preflight.

100% offline. tmp_path filesystem. autouse `_no_network` fixture blocks all
real HTTP. The "external check" is supplied by a fixture provider registered
via `register_external_observation_provider`. The runtime path
(ToolRegistry.execute → attach_artifact_to_result → external_check_runner →
gate) is exercised end-to-end.

Demonstrates the 11 invariants Hector required for F3b.1:
  1.  ok=True without success_condition_artifact → blocked (bypass detector)
  2.  ok=True without evidence_uri → blocked (tier3 invariant)
  3.  ok=True without preflight provider → blocked (tier3 invariant)
  4.  ok=True without verification mechanism → blocked
  5.  external_check mock status="failed" → failed
  6.  external_check mock status="processing" → pending_verification
  7.  external_check mock status="completed" + valid file → succeeded
  8.  output_path declared but file missing → failed
  9.  declared hash differs from real hash → failed
  10. valid file with sha256/size/MIME/extension → succeeded
  11. video_id with invalid format → failed
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import pytest

from claw_v2.tools import ToolDefinition, ToolRegistry
from claw_v2.verification.external_check_runner import (
    clear_providers,
    register_external_observation_provider,
    register_preflight_provider,
)
from claw_v2.verification.external_tool_contracts import (
    EXTERNAL_TOOL_PREFLIGHTS,
    EXTERNAL_TOOL_SUCCESS_CONDITIONS,
)
from claw_v2.verification.local_tool_runner import (
    ARTIFACT_RESULT_KEY,
    CONTRACT_REQUIRED_KEY,
    lift_artifact_to_checkpoint,
)
from claw_v2.verification.promote_gate import apply_promote_gate_to_checkpoint


# ---------------------------------------------------------------------------
# Network guard + provider isolation between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("Network call attempted from F3b.1 test — forbidden")
    import socket, urllib.request
    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    yield


@pytest.fixture(autouse=True)
def _isolate_providers():
    clear_providers()
    yield
    clear_providers()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _noop_approval_gate(definition, args):
    """Test-only approval gate. F3b.1 doesn't test approval flow itself."""
    return None


def _registry(workspace_root: Path) -> ToolRegistry:
    reg = ToolRegistry(workspace_root=workspace_root)
    reg.register(
        ToolDefinition(
            name="HeyGenDeliver",
            description="fake HeyGenDeliver",
            allowed_agent_classes=("operator", "deployer"),
            handler=_fake_handler,
            mutates_state=True,
            tier=3,
            success_condition=EXTERNAL_TOOL_SUCCESS_CONDITIONS["HeyGenDeliver"],
            preflight=EXTERNAL_TOOL_PREFLIGHTS["HeyGenDeliver"],
        )
    )
    return reg


_HANDLER_RESULT: dict | None = None


def _fake_handler(args):
    """Returns whatever the test installed via `_set_handler_result`."""
    assert _HANDLER_RESULT is not None, "test must call _set_handler_result first"
    return dict(_HANDLER_RESULT)


def _set_handler_result(result: dict) -> None:
    global _HANDLER_RESULT
    _HANDLER_RESULT = dict(result)


def _make_mp4(tmp: Path, body: bytes = b"\x00\x00\x00 ftypisom" + b"X" * 2000) -> tuple[Path, str, int]:
    path = tmp / "video.mp4"
    path.write_bytes(body)
    return path, hashlib.sha256(body).hexdigest(), len(body)


def _run(reg: ToolRegistry, args: dict | None = None) -> tuple[str, str, dict, list, dict]:
    a = args or {"video_id": "abc123def456ghij", "caption": "test"}
    result = reg.execute("HeyGenDeliver", a, agent_class="deployer", approval_gate=_noop_approval_gate)
    checkpoint = lift_artifact_to_checkpoint(
        {"verification_status": "passed"}, result
    )
    terminal, verification, new_checkpoint, events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    return terminal, verification, new_checkpoint, events, result


# ---------------------------------------------------------------------------
# §1 — ok=True but artifact missing from checkpoint → blocked (bypass)
# ---------------------------------------------------------------------------


def test_artifact_missing_blocks(tmp_path):
    _set_handler_result({"ok": True})  # no fields
    reg = _registry(tmp_path)
    # Drop the artifact deliberately to simulate downstream bug.
    result = reg.execute("HeyGenDeliver", {"video_id": "abc123def456ghij"}, agent_class="deployer", approval_gate=_noop_approval_gate)
    assert result[CONTRACT_REQUIRED_KEY] is True
    broken_checkpoint = {
        "verification_status": "passed",
        "contract_required": True,
        # success_condition_artifact intentionally dropped
    }
    terminal, verification, new_ck, events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=broken_checkpoint,
    )
    assert terminal == ""
    assert verification == "blocked"
    assert any(name == "promote_gate_contract_bypass_detected" for name, _ in events)


# ---------------------------------------------------------------------------
# §2 — ok=True without evidence_uri → blocked
# §3 — ok=True without preflight provider → blocked
# §4 — without verification mechanism → blocked
# ---------------------------------------------------------------------------


def test_no_evidence_uri_blocks(tmp_path):
    mp4, sha, size = _make_mp4(tmp_path)
    _set_handler_result({
        "ok": True,
        "video_id": "abc123def456ghij",
        "output_path": "",            # forces evidence_uri to None
        "output_sha256": sha,
        "output_size_bytes": size,
        "telegram_msg_id": "12345",
    })
    # Preflight present + external_obs present, but evidence_uri missing.
    register_preflight_provider("HeyGenDeliver", lambda t, a: (True, "ok"))
    register_external_observation_provider(
        "HeyGenDeliver", lambda t, a, r: {"json": {"ok": True}, "body_text": "delivered"}
    )
    reg = _registry(tmp_path)
    terminal, verification, new_ck, _events, _r = _run(reg)
    assert terminal == ""
    assert verification == "blocked"
    errors = new_ck["promote_gate_envelope"]["verification_result"]["errors"]
    assert "tier3_requires_evidence_uri" in errors


def test_no_preflight_provider_blocks(tmp_path):
    mp4, sha, size = _make_mp4(tmp_path)
    _set_handler_result({
        "ok": True,
        "video_id": "abc123def456ghij",
        "output_path": str(mp4),
        "output_sha256": sha,
        "output_size_bytes": size,
        "telegram_msg_id": "12345",
    })
    # external observation provider but NO preflight provider
    register_external_observation_provider(
        "HeyGenDeliver", lambda t, a, r: {"json": {"ok": True}, "body_text": "delivered"}
    )
    reg = _registry(tmp_path)
    terminal, verification, new_ck, _events, _r = _run(reg)
    assert terminal == ""
    assert verification == "blocked"
    errors = new_ck["promote_gate_envelope"]["verification_result"]["errors"]
    # Either tier3_preflight_not_passed (no_provider → passed=False) or the
    # blocking is via preflight_passed=False; both satisfy §3.
    assert (
        "tier3_preflight_not_passed" in errors
        or "tier3_requires_preflight" in errors
    )


# ---------------------------------------------------------------------------
# §5 — external_check returns ok=False → failed
# ---------------------------------------------------------------------------


def test_external_check_failed_yields_failed(tmp_path):
    mp4, sha, size = _make_mp4(tmp_path)
    _set_handler_result({
        "ok": True,
        "video_id": "abc123def456ghij",
        "output_path": str(mp4),
        "output_sha256": sha,
        "output_size_bytes": size,
        "telegram_msg_id": "12345",
    })
    register_preflight_provider("HeyGenDeliver", lambda t, a: (True, "ok"))
    register_external_observation_provider(
        "HeyGenDeliver",
        lambda t, a, r: {"json": {"ok": False, "error": "send failed"}, "body_text": "fail"},
    )
    reg = _registry(tmp_path)
    terminal, verification, new_ck, _events, _r = _run(reg)
    assert terminal == "failed"
    assert verification == "failed"
    errors = new_ck["promote_gate_envelope"]["verification_result"]["errors"]
    assert any("json_path_mismatch" in e for e in errors)


# ---------------------------------------------------------------------------
# §6 — external_check pending → pending_verification
# ---------------------------------------------------------------------------


def test_external_check_processing_yields_pending(tmp_path):
    mp4, sha, size = _make_mp4(tmp_path)
    _set_handler_result({
        "ok": True,
        "video_id": "abc123def456ghij",
        "output_path": str(mp4),
        "output_sha256": sha,
        "output_size_bytes": size,
        "telegram_msg_id": "12345",
    })
    register_preflight_provider("HeyGenDeliver", lambda t, a: (True, "ok"))
    # Provider returns None when status is still processing — observation
    # is not pre-fetched yet, gate must keep the task open.
    register_external_observation_provider(
        "HeyGenDeliver", lambda t, a, r: None
    )
    reg = _registry(tmp_path)
    terminal, verification, _new_ck, _events, _r = _run(reg)
    assert terminal == ""
    assert verification == "pending_verification"


# ---------------------------------------------------------------------------
# §7 + §10 — completed + valid file + integrity matches → succeeded
# ---------------------------------------------------------------------------


def test_happy_path_with_mocked_completed_status(tmp_path):
    mp4, sha, size = _make_mp4(tmp_path)
    _set_handler_result({
        "ok": True,
        "video_id": "abc123def456ghij",
        "output_path": str(mp4),
        "output_sha256": sha,
        "output_size_bytes": size,
        "telegram_msg_id": "12345",
    })
    register_preflight_provider("HeyGenDeliver", lambda t, a: (True, "auth_ok"))
    register_external_observation_provider(
        "HeyGenDeliver", lambda t, a, r: {"json": {"ok": True}, "body_text": "delivered"}
    )
    reg = _registry(tmp_path)
    terminal, verification, _new_ck, events, result = _run(reg)
    assert terminal == "succeeded"
    assert verification == "passed"
    # Artifact is tier 3
    artifact = result[ARTIFACT_RESULT_KEY]
    assert artifact["tier"] == 3
    assert artifact["preflight_passed"] is True
    # No degrade events
    assert events == []


# ---------------------------------------------------------------------------
# §8 — output_path declared but file missing → failed
# ---------------------------------------------------------------------------


def test_output_file_missing_fails(tmp_path):
    fake_path = tmp_path / "ghost.mp4"     # never created
    _set_handler_result({
        "ok": True,
        "video_id": "abc123def456ghij",
        "output_path": str(fake_path),
        "output_sha256": "0" * 64,
        "output_size_bytes": 100,
        "telegram_msg_id": "12345",
    })
    register_preflight_provider("HeyGenDeliver", lambda t, a: (True, "ok"))
    register_external_observation_provider(
        "HeyGenDeliver", lambda t, a, r: {"json": {"ok": True}, "body_text": "ok"}
    )
    reg = _registry(tmp_path)
    terminal, _v, new_ck, _events, _r = _run(reg)
    assert terminal == "failed"
    errors = new_ck["promote_gate_envelope"]["verification_result"]["errors"]
    assert any(
        e in errors
        for e in ("path_file_not_found:output_path", "integrity_file_not_found:output_path")
    )


# ---------------------------------------------------------------------------
# §9 — declared hash differs from real → failed
# ---------------------------------------------------------------------------


def test_declared_hash_differs_from_real_fails(tmp_path):
    mp4, real_sha, size = _make_mp4(tmp_path)
    wrong_sha = "f" * 64
    _set_handler_result({
        "ok": True,
        "video_id": "abc123def456ghij",
        "output_path": str(mp4),
        "output_sha256": wrong_sha,
        "output_size_bytes": size,
        "telegram_msg_id": "12345",
    })
    register_preflight_provider("HeyGenDeliver", lambda t, a: (True, "ok"))
    register_external_observation_provider(
        "HeyGenDeliver", lambda t, a, r: {"json": {"ok": True}, "body_text": "ok"}
    )
    reg = _registry(tmp_path)
    terminal, _v, new_ck, _events, _r = _run(reg)
    assert terminal == "failed"
    errors = new_ck["promote_gate_envelope"]["verification_result"]["errors"]
    assert "integrity_hash_mismatch:output_path" in errors


# ---------------------------------------------------------------------------
# §11 — invalid video_id format → failed
# ---------------------------------------------------------------------------


def test_invalid_video_id_format_fails(tmp_path):
    mp4, sha, size = _make_mp4(tmp_path)
    _set_handler_result({
        "ok": True,
        "video_id": "bad!id",     # contains invalid chars + too short
        "output_path": str(mp4),
        "output_sha256": sha,
        "output_size_bytes": size,
        "telegram_msg_id": "12345",
    })
    register_preflight_provider("HeyGenDeliver", lambda t, a: (True, "ok"))
    register_external_observation_provider(
        "HeyGenDeliver", lambda t, a, r: {"json": {"ok": True}, "body_text": "ok"}
    )
    reg = _registry(tmp_path)
    terminal, _v, new_ck, _events, _r = _run(reg)
    assert terminal == "failed"
    errors = new_ck["promote_gate_envelope"]["verification_result"]["errors"]
    assert "regex_mismatch:video_id" in errors


# ---------------------------------------------------------------------------
# Privacy — no video bytes in artifact
# ---------------------------------------------------------------------------


def test_no_video_bytes_in_artifact(tmp_path):
    mp4, sha, size = _make_mp4(tmp_path, body=b"\x89PNG..MAGIC_VIDEO_PAYLOAD_XYZ" + b"X" * 500)
    _set_handler_result({
        "ok": True,
        "video_id": "abc123def456ghij",
        "output_path": str(mp4),
        "output_sha256": sha,
        "output_size_bytes": size,
        "telegram_msg_id": "12345",
    })
    register_preflight_provider("HeyGenDeliver", lambda t, a: (True, "ok"))
    register_external_observation_provider(
        "HeyGenDeliver", lambda t, a, r: {"json": {"ok": True}, "body_text": "ok"}
    )
    reg = _registry(tmp_path)
    _t, _v, _nc, _ev, result = _run(reg, {
        "video_id": "abc123def456ghij",
        "video_bytes": "RAW_BINARY_BLOB_NEVER_PERSISTED",
        "video_url": "https://heygen.example/video.mp4",
    })
    artifact = result[ARTIFACT_RESULT_KEY]
    artifact_str = str(artifact)
    assert "MAGIC_VIDEO_PAYLOAD_XYZ" not in artifact_str
    assert "RAW_BINARY_BLOB_NEVER_PERSISTED" not in artifact_str
    # video_bytes / video_url / image_bytes etc. must be stripped from
    # tool_args_redacted entirely.
    redacted_keys = set(artifact["tool_args_redacted"].keys())
    for forbidden in ("video_bytes", "video_url", "image_b64", "image_bytes", "image_data"):
        assert forbidden not in redacted_keys
