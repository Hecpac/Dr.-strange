"""F3b.0 — Tier-3 external contract tests.

100% offline. tmp_path fs + mocked external_observation. Autouse
`_no_network` fixture blocks sockets/urllib. NO real HeyGen / Telegram /
OpenAI / GitHub / browser / CDP calls.

Demonstrates that no Tier 3 tool can ever land `succeeded` from
tool.ok=True alone — the gate requires preflight + external_check +
evidence per F2.5+F2.5.1+F3a.1 invariants.
"""

from __future__ import annotations

import hashlib
import pytest

from claw_v2.verification.external_tool_contracts import (
    EXTERNAL_TOOL_PREFLIGHTS,
    EXTERNAL_TOOL_SUCCESS_CONDITIONS,
    EXTERNAL_TOOL_REDACTED_KEYS,
)
from claw_v2.verification.local_tool_contracts import (
    build_local_tool_artifact,
)
from claw_v2.verification.promote_gate import apply_promote_gate_to_checkpoint


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("Network call attempted from F3b.0 test — forbidden")

    import socket
    import urllib.request

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    yield


def _build_and_gate(
    tool_name,
    tool_args,
    tool_result,
    *,
    external_observation=None,
    state_delta_observation=None,
    evidence_uri=None,
    allowed_path_roots=(),
    preflight_passed=True,
):
    """Build an artifact + ride it through the promote gate."""
    artifact = build_local_tool_artifact(
        tool_name=tool_name,
        tool_args=tool_args,
        tool_result=tool_result,
        state_delta_observation=state_delta_observation,
        evidence_uri=evidence_uri,
        external_observation=external_observation,
        allowed_path_roots=allowed_path_roots,
        preflight_passed=preflight_passed,
    )
    checkpoint = {
        "verification_status": "passed",
        "contract_required": True,
        "success_condition_artifact": artifact,
    }
    return apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    ), artifact


# ===========================================================================
# Contract registry sanity
# ===========================================================================


@pytest.mark.parametrize(
    "tool", ["HeyGenDeliver", "HeyGenVideo", "GPTImage", "SkillExecute", "A2ASend", "WikiDelete"]
)
def test_external_tool_has_contract(tool):
    assert tool in EXTERNAL_TOOL_SUCCESS_CONDITIONS
    assert tool in EXTERNAL_TOOL_PREFLIGHTS


@pytest.mark.parametrize("tool", ["HeyGenDeliver", "HeyGenVideo", "GPTImage", "A2ASend"])
def test_external_check_declared(tool):
    sc = EXTERNAL_TOOL_SUCCESS_CONDITIONS[tool]
    # All four require external state confirmation
    if tool in {"HeyGenDeliver", "HeyGenVideo", "GPTImage", "A2ASend"}:
        if tool == "GPTImage":
            # GPTImage uses file integrity instead of HTTP external_check
            assert sc.verify_file_integrity
        else:
            assert sc.external_check is not None


def test_redaction_list_covers_sensitive_keys():
    assert "prompt" in EXTERNAL_TOOL_REDACTED_KEYS
    assert "image_bytes" in EXTERNAL_TOOL_REDACTED_KEYS
    assert "video_bytes" in EXTERNAL_TOOL_REDACTED_KEYS
    assert "api_key" in EXTERNAL_TOOL_REDACTED_KEYS


# ===========================================================================
# HeyGenDeliver — must download file + send to Telegram
# ===========================================================================


def test_heygendeliver_ok_but_no_external_observation_pending(tmp_path):
    """tool.ok=True with no external_observation → pending_verification."""
    mp4 = tmp_path / "video.mp4"
    body = b"FAKE_MP4_HEADER\x00" * 1000
    mp4.write_bytes(body)
    real_sha = hashlib.sha256(body).hexdigest()

    (terminal, verification, _nc, _ev), _ = _build_and_gate(
        "HeyGenDeliver",
        {"video_id": "abc123def456ghij", "caption": "test"},
        {
            "ok": True,
            "video_id": "abc123def456ghij",
            "output_path": str(mp4),
            "output_sha256": real_sha,
            "output_size_bytes": len(body),
            "telegram_msg_id": "12345",
        },
        external_observation=None,  # NOT pre-fetched
        evidence_uri=str(mp4),
        allowed_path_roots=(str(tmp_path),),
    )
    assert terminal != "succeeded"
    assert verification in {"pending_verification", "blocked"}


def test_heygendeliver_external_check_failed_yields_failed(tmp_path):
    mp4 = tmp_path / "video.mp4"
    body = b"FAKE_MP4\x00" * 1000
    mp4.write_bytes(body)
    real_sha = hashlib.sha256(body).hexdigest()

    # External observation says Telegram returned ok=false
    bad_obs = {"body_text": "", "json": {"ok": False}}

    (terminal, verification, new_ck, _ev), _ = _build_and_gate(
        "HeyGenDeliver",
        {"video_id": "abc123def456ghij"},
        {
            "ok": True,
            "video_id": "abc123def456ghij",
            "output_path": str(mp4),
            "output_sha256": real_sha,
            "output_size_bytes": len(body),
            "telegram_msg_id": "12345",
        },
        external_observation=bad_obs,
        evidence_uri=str(mp4),
        allowed_path_roots=(str(tmp_path),),
    )
    assert terminal == "failed"
    assert verification == "failed"
    errors = new_ck["promote_gate_envelope"]["verification_result"]["errors"]
    assert any("json_path_mismatch" in e for e in errors)


def test_heygendeliver_artifact_missing_blocks(tmp_path):
    """If artifact were dropped (simulating downstream bug), bypass detector fires."""
    broken = {
        "verification_status": "passed",
        "contract_required": True,
        # success_condition_artifact intentionally missing
    }
    terminal, verification, new_ck, events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=broken,
    )
    assert terminal == ""
    assert verification == "blocked"
    assert any(name == "promote_gate_contract_bypass_detected" for name, _ in events)


def test_heygendeliver_output_file_missing_fails(tmp_path):
    """tool.ok=True but the declared output_path does not exist on disk."""
    fake_path = tmp_path / "ghost.mp4"  # never created
    (terminal, verification, new_ck, _ev), _ = _build_and_gate(
        "HeyGenDeliver",
        {"video_id": "abc123def456ghij"},
        {
            "ok": True,
            "video_id": "abc123def456ghij",
            "output_path": str(fake_path),
            "output_sha256": "0" * 64,
            "output_size_bytes": 42,
            "telegram_msg_id": "12345",
        },
        external_observation={"body_text": "ok", "json": {"ok": True}},
        evidence_uri=str(fake_path),
        allowed_path_roots=(str(tmp_path),),
    )
    assert terminal == "failed"
    errors = new_ck["promote_gate_envelope"]["verification_result"]["errors"]
    assert any(
        "path_file_not_found:output_path" == e or "integrity_file_not_found:output_path" == e
        for e in errors
    )


def test_heygendeliver_declared_hash_differs_from_real_fails(tmp_path):
    mp4 = tmp_path / "v.mp4"
    body = b"X" * 500
    mp4.write_bytes(body)
    # Declare a DIFFERENT hash
    wrong_sha = "f" * 64

    (terminal, _v, new_ck, _ev), _ = _build_and_gate(
        "HeyGenDeliver",
        {"video_id": "abc123def456ghij"},
        {
            "ok": True,
            "video_id": "abc123def456ghij",
            "output_path": str(mp4),
            "output_sha256": wrong_sha,
            "output_size_bytes": len(body),
            "telegram_msg_id": "12345",
        },
        external_observation={"body_text": "ok", "json": {"ok": True}},
        evidence_uri=str(mp4),
        allowed_path_roots=(str(tmp_path),),
    )
    assert terminal == "failed"
    errors = new_ck["promote_gate_envelope"]["verification_result"]["errors"]
    assert "integrity_hash_mismatch:output_path" in errors


def test_heygendeliver_happy_path_with_mocked_observation(tmp_path):
    mp4 = tmp_path / "ok.mp4"
    body = b"\x00\x00\x00 ftypisom" + b"X" * 2000
    mp4.write_bytes(body)
    real_sha = hashlib.sha256(body).hexdigest()

    (terminal, verification, _nc, _ev), artifact = _build_and_gate(
        "HeyGenDeliver",
        {"video_id": "abc123def456ghij", "caption": "Test caption"},
        {
            "ok": True,
            "video_id": "abc123def456ghij",
            "output_path": str(mp4),
            "output_sha256": real_sha,
            "output_size_bytes": len(body),
            "telegram_msg_id": "12345",
        },
        external_observation={"body_text": "delivered", "json": {"ok": True}},
        evidence_uri=str(mp4),
        allowed_path_roots=(str(tmp_path),),
    )
    assert terminal == "succeeded"
    assert verification == "passed"
    # Artifact must be tier 3 + carry preflight spec
    assert artifact["tier"] == 3
    assert artifact["preflight"] is not None


# ===========================================================================
# HeyGenVideo — only succeeded if external status=="completed"
# ===========================================================================


def test_heygenvideo_pending_when_no_observation():
    (terminal, verification, _nc, _ev), _ = _build_and_gate(
        "HeyGenVideo",
        {"text": "long script content here"},
        {"ok": True, "video_id": "v1d3o_id_abc12345", "status": "processing"},
        external_observation=None,
        evidence_uri="artifacts/test/dummy.json",
    )
    assert terminal != "succeeded"
    assert verification == "pending_verification"


def test_heygenvideo_status_not_completed_fails():
    (terminal, _v, new_ck, _ev), _ = _build_and_gate(
        "HeyGenVideo",
        {"text": "..."},
        {"ok": True, "video_id": "v1d3o_id_abc12345", "status": "processing"},
        external_observation={"body_text": "polled", "json": {"status": "processing"}},
        evidence_uri="artifacts/test/dummy.json",
    )
    assert terminal == "failed"
    errors = new_ck["promote_gate_envelope"]["verification_result"]["errors"]
    assert any("json_path_mismatch:status" == e for e in errors)


def test_heygenvideo_text_arg_never_in_artifact():
    """Privacy: the speech text MUST NOT land in the artifact."""
    sensitive = "Hector's private speech full of identifiable details ABC123XYZ"
    _, artifact = _build_and_gate(
        "HeyGenVideo",
        {"text": sensitive},
        {"ok": True, "video_id": "v1d3o_id_abc12345", "status": "completed"},
        external_observation={"body_text": "ok", "json": {"status": "completed"}},
    )
    assert "ABC123XYZ" not in str(artifact)
    assert "text" not in artifact["tool_args_redacted"]


def test_heygenvideo_happy_path():
    (terminal, verification, _nc, _ev), _ = _build_and_gate(
        "HeyGenVideo",
        {"text": "..."},
        {"ok": True, "video_id": "v1d3o_id_abc12345", "status": "completed"},
        external_observation={"body_text": "ok", "json": {"status": "completed"}},
        evidence_uri="artifacts/test/heygen_video.json",
    )
    assert terminal == "succeeded"
    assert verification == "passed"


# ===========================================================================
# GPTImage — local file integrity is the gate
# ===========================================================================


def test_gptimage_no_file_fails(tmp_path):
    (terminal, _v, new_ck, _ev), _ = _build_and_gate(
        "GPTImage",
        {"prompt": "secret prompt that must not leak"},
        {
            "ok": True,
            "output_path": str(tmp_path / "missing.png"),
            "mime_type": "image/png",
            "size_bytes": 1234,
            "output_sha256": "0" * 64,
        },
        external_observation=None,
        evidence_uri=str(tmp_path / "missing.png"),
        allowed_path_roots=(str(tmp_path),),
    )
    assert terminal == "failed"


def test_gptimage_prompt_never_in_artifact(tmp_path):
    png = tmp_path / "out.png"
    body = b"\x89PNG\r\n\x1a\n" + b"X" * 100
    png.write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    sensitive = "TOPSECRETPROMPTIDENTIFIER-XYZ"
    _, artifact = _build_and_gate(
        "GPTImage",
        {"prompt": sensitive, "size": "1024x1024"},
        {
            "ok": True,
            "output_path": str(png),
            "mime_type": "image/png",
            "size_bytes": len(body),
            "output_sha256": sha,
        },
        external_observation=None,
        evidence_uri=str(png),
        allowed_path_roots=(str(tmp_path),),
    )
    assert sensitive not in str(artifact)
    assert "prompt" not in artifact["tool_args_redacted"]


def test_gptimage_mime_must_match_regex(tmp_path):
    png = tmp_path / "out.bin"
    body = b"\x00" * 50
    png.write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    (terminal, _v, new_ck, _ev), _ = _build_and_gate(
        "GPTImage",
        {"prompt": "p"},
        {
            "ok": True,
            "output_path": str(png),
            "mime_type": "application/octet-stream",  # invalid mime
            "size_bytes": len(body),
            "output_sha256": sha,
        },
        evidence_uri=str(png),
        allowed_path_roots=(str(tmp_path),),
    )
    assert terminal == "failed"
    errors = new_ck["promote_gate_envelope"]["verification_result"]["errors"]
    assert "regex_mismatch:mime_type" in errors


def test_gptimage_happy_path(tmp_path):
    png = tmp_path / "ok.png"
    body = b"\x89PNG\r\n\x1a\n" + b"GOOD" * 100
    png.write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    (terminal, verification, _nc, _ev), _ = _build_and_gate(
        "GPTImage",
        {"prompt": "..."},
        {
            "ok": True,
            "output_path": str(png),
            "mime_type": "image/png",
            "size_bytes": len(body),
            "output_sha256": sha,
        },
        evidence_uri=str(png),
        allowed_path_roots=(str(tmp_path),),
    )
    assert terminal == "succeeded"
    assert verification == "passed"


# ===========================================================================
# A2ASend / SkillExecute / WikiDelete — minimal contracts
# ===========================================================================


def test_a2asend_delivered_false_fails():
    (terminal, _v, new_ck, _ev), _ = _build_and_gate(
        "A2ASend",
        {"to_agent": "peer1", "action": "ping"},
        {"ok": True, "to_agent": "peer1", "task_id": "tid-1", "delivered": False},
        external_observation={"body_text": "x", "json": {"received": True}},
        evidence_uri="artifacts/test/dummy.json",
    )
    assert terminal == "failed"
    errors = new_ck["promote_gate_envelope"]["verification_result"]["errors"]
    assert any("must_equal_mismatch:delivered" == e for e in errors)


def test_a2asend_no_external_observation_pending():
    (terminal, verification, _nc, _ev), _ = _build_and_gate(
        "A2ASend",
        {"to_agent": "peer1", "action": "ping"},
        {"ok": True, "to_agent": "peer1", "task_id": "tid-1", "delivered": True},
        external_observation=None,
        evidence_uri="artifacts/test/dummy.json",
    )
    assert terminal != "succeeded"
    assert verification == "pending_verification"


def test_a2asend_happy_path():
    (terminal, verification, _nc, _ev), _ = _build_and_gate(
        "A2ASend",
        {"to_agent": "peer1", "action": "ping"},
        {"ok": True, "to_agent": "peer1", "task_id": "tid-1", "delivered": True},
        external_observation={"body_text": "ok", "json": {"received": True}},
        evidence_uri="artifacts/test/dummy.json",
    )
    assert terminal == "succeeded"


def test_skillexecute_blocked_without_verification_spec():
    """SkillExecute (Tier 3) without external_check OR file_integrity OR
    state_delta_check is BLOCKED by the gate's tier-3 invariant —
    'tier3_requires_external_verification'. This documents the F3b.0 design:
    a verification mechanism for SkillExecute is debt for F3b.1."""
    (terminal, verification, new_ck, _ev), _ = _build_and_gate(
        "SkillExecute",
        {"name": "my_skill"},
        {"ok": True, "skill_name": "my_skill", "execution_id": "exec_abc123"},
        evidence_uri="artifacts/test/skill_exec.json",
    )
    assert terminal == ""
    assert verification == "blocked"
    errors = new_ck["promote_gate_envelope"]["verification_result"]["errors"]
    assert "tier3_requires_external_verification" in errors


def test_wikidelete_blocked_without_verification_spec():
    """WikiDelete (Tier 3) — same as SkillExecute. Verification mechanism for
    the cascade-deletion (e.g. confirmation that the slug is gone from the
    wiki store) is debt for F3b.1."""
    (terminal, verification, _new_ck, _ev), _ = _build_and_gate(
        "WikiDelete",
        {"slug": "stale-page"},
        {"ok": True, "slug": "stale-page", "deleted": True, "approval_artifact": "approval-001"},
        evidence_uri="artifacts/test/wiki_delete.json",
    )
    assert terminal == ""
    assert verification == "blocked"


def test_wikidelete_missing_approval_artifact_still_blocks():
    """Even with empty approval_artifact, the gate still blocks first via
    tier-3 verification invariant — must_be_nonempty_str fires at the
    envelope-error level but the blocked reason wins."""
    (terminal, verification, _new_ck, _ev), _ = _build_and_gate(
        "WikiDelete",
        {"slug": "stale-page"},
        {"ok": True, "slug": "stale-page", "deleted": True, "approval_artifact": ""},
        evidence_uri="artifacts/test/wiki_delete.json",
    )
    # Either blocked (tier-3 invariant) or failed (must_be_nonempty_str).
    # In F3b.0 the tier-3 invariant runs first → blocked.
    assert terminal != "succeeded"


# ===========================================================================
# Privacy — no sensitive content in artifact across all Tier 3 tools
# ===========================================================================


@pytest.mark.parametrize(
    "tool,args",
    [
        ("HeyGenVideo", {"text": "SECRET-TEXT-XYZ", "avatar_id": "a1"}),
        ("GPTImage", {"prompt": "SECRET-PROMPT-XYZ", "size": "1024x1024"}),
        ("HeyGenDeliver", {"video_id": "abc123def456ghij", "image_b64": "SECRET-XYZ"}),
    ],
)
def test_privacy_no_sensitive_args_in_artifact(tool, args, tmp_path):
    # Provide minimal tool_result to make build succeed
    result = {"ok": True}
    if tool == "HeyGenVideo":
        result.update({"video_id": "abc123def456ghij", "status": "completed"})
    elif tool == "GPTImage":
        f = tmp_path / "x.png"
        f.write_bytes(b"\x89PNG" + b"X" * 100)
        result.update(
            {
                "output_path": str(f),
                "mime_type": "image/png",
                "size_bytes": f.stat().st_size,
                "output_sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
            }
        )
    elif tool == "HeyGenDeliver":
        f = tmp_path / "v.mp4"
        f.write_bytes(b"X" * 200)
        result.update(
            {
                "video_id": "abc123def456ghij",
                "output_path": str(f),
                "output_sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
                "output_size_bytes": f.stat().st_size,
                "telegram_msg_id": "12345",
            }
        )

    artifact = build_local_tool_artifact(
        tool_name=tool,
        tool_args=args,
        tool_result=result,
        state_delta_observation=None,
        evidence_uri=None,
        external_observation=None,
        allowed_path_roots=(str(tmp_path),) if tool != "HeyGenVideo" else (),
    )
    assert "SECRET-TEXT-XYZ" not in str(artifact)
    assert "SECRET-PROMPT-XYZ" not in str(artifact)
    assert "SECRET-XYZ" not in str(artifact)
    # tool_args_redacted should not contain the sensitive arg names
    for k in ("text", "prompt", "image_b64"):
        assert k not in artifact["tool_args_redacted"]
