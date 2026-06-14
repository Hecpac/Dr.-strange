"""F3a-ext.1 — semantic hardening tests.

Offline only. tmp_path fs. Autouse `_no_network` fixture.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import pytest

from claw_v2.tools import ToolDefinition, ToolRegistry
from claw_v2.verification.local_tool_contracts import (
    LOCAL_TOOL_SUCCESS_CONDITIONS,
)
from claw_v2.verification.local_tool_runner import (
    ARTIFACT_RESULT_KEY,
    lift_artifact_to_checkpoint,
)
from claw_v2.verification.promote_gate import apply_promote_gate_to_checkpoint


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("Network call attempted from ext_hardening — forbidden")
    import socket
    import urllib.request
    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    yield


def _registry_with(tool_name: str, handler, workspace_root: Path):
    reg = ToolRegistry(workspace_root=workspace_root)
    reg.register(
        ToolDefinition(
            name=tool_name,
            description=f"fake {tool_name}",
            allowed_agent_classes=("operator",),
            handler=handler,
            mutates_state=True,
            tier=2,
            success_condition=LOCAL_TOOL_SUCCESS_CONDITIONS[tool_name],
        )
    )
    return reg


def _run_and_gate(reg, tool_name, args):
    result = reg.execute(tool_name, args, agent_class="operator")
    checkpoint = lift_artifact_to_checkpoint({"verification_status": "passed"}, result)
    return apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    ), result


# ===========================================================================
# A. SkillGenerate — hash + size + path integrity
# ===========================================================================


def test_skillgen_happy_path_with_real_hash(tmp_path):
    out = tmp_path / "skills" / "ok.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    body = b"# real content\n"
    out.write_bytes(body)
    real_sha = hashlib.sha256(body).hexdigest()

    def _h(args):
        return {"ok": True, "name": "ok", "path": str(out), "size_bytes": len(body), "sha256_hash": real_sha}

    reg = _registry_with("SkillGenerate", _h, workspace_root=tmp_path)
    (terminal, verification, _nc, _ev), _r = _run_and_gate(reg, "SkillGenerate", {"task": "x"})
    assert terminal == "succeeded"
    assert verification == "passed"


def test_skillgen_declared_hash_differs_from_real_fails(tmp_path):
    out = tmp_path / "skills" / "tampered.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"real content")
    fake_sha = "0" * 64

    def _h(args):
        return {"ok": True, "name": "x", "path": str(out), "size_bytes": 12, "sha256_hash": fake_sha}

    reg = _registry_with("SkillGenerate", _h, workspace_root=tmp_path)
    (terminal, _v, new_ck, _ev), _r = _run_and_gate(reg, "SkillGenerate", {"task": "x"})
    assert terminal == "failed"
    assert "integrity_hash_mismatch:path" in new_ck["promote_gate_envelope"]["verification_result"]["errors"]


def test_skillgen_declared_size_differs_from_real_fails(tmp_path):
    out = tmp_path / "skills" / "sizebad.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    body = b"twelve bytes"      # 12 bytes
    out.write_bytes(body)
    real_sha = hashlib.sha256(body).hexdigest()

    def _h(args):
        return {"ok": True, "name": "x", "path": str(out), "size_bytes": 999, "sha256_hash": real_sha}

    reg = _registry_with("SkillGenerate", _h, workspace_root=tmp_path)
    (terminal, _v, new_ck, _ev), _r = _run_and_gate(reg, "SkillGenerate", {"task": "x"})
    assert terminal == "failed"
    assert "integrity_size_mismatch:path" in new_ck["promote_gate_envelope"]["verification_result"]["errors"]


def test_skillgen_path_outside_allowed_root_fails(tmp_path):
    """The path lives OUTSIDE the workspace_root (and outside /tmp). Must fail."""
    # Pick a path under root that is NEITHER tmp_path NOR /tmp.
    outside_dir = Path("/private/etc")    # exists on macOS, never under tmp_path or /tmp prefix
    outside_path = outside_dir / "hosts"  # exists on macOS but outside our roots
    if not outside_path.exists():
        pytest.skip("test depends on /private/etc/hosts existing")
    body = outside_path.read_bytes()
    real_sha = hashlib.sha256(body).hexdigest()
    real_size = outside_path.stat().st_size

    def _h(args):
        return {
            "ok": True, "name": "x",
            "path": str(outside_path),
            "size_bytes": real_size,
            "sha256_hash": real_sha,
        }

    reg = _registry_with("SkillGenerate", _h, workspace_root=tmp_path)
    (terminal, _v, new_ck, _ev), _r = _run_and_gate(reg, "SkillGenerate", {"task": "x"})
    assert terminal == "failed"
    errors = new_ck["promote_gate_envelope"]["verification_result"]["errors"]
    assert (
        "path_outside_allowed_root:path" in errors
        or "integrity_path_outside_allowed_root:path" in errors
    )


def test_skillgen_truncated_16hex_sha_fails(tmp_path):
    out = tmp_path / "skills" / "short.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"x\n")

    def _h(args):
        return {"ok": True, "name": "x", "path": str(out), "size_bytes": 2, "sha256_hash": "deadbeefdeadbeef"}

    reg = _registry_with("SkillGenerate", _h, workspace_root=tmp_path)
    (terminal, _v, new_ck, _ev), _r = _run_and_gate(reg, "SkillGenerate", {"task": "x"})
    assert terminal == "failed"
    assert "regex_mismatch:sha256_hash" in new_ck["promote_gate_envelope"]["verification_result"]["errors"]


# ===========================================================================
# B. Bash git_commit — direct semantic validation
# ===========================================================================


def test_git_commit_main_branch_without_reason_fails(tmp_path):
    """Even if handler omits reason='protected_branch_detected', branch=main
    must directly fail via forbidden_field_values."""

    def _h(args):
        return {
            "ok": True, "exit_code": 0,
            "branch": "main",
            "before_head": "0" * 40, "after_head": "a" * 40, "commit_hash": "a" * 40,
        }

    reg = _registry_with("Bash", _h, workspace_root=tmp_path)
    (terminal, _v, new_ck, _ev), _r = _run_and_gate(
        reg, "Bash", {"command": "git commit", "command_kind": "git_commit"}
    )
    assert terminal == "failed"
    errors = new_ck["promote_gate_envelope"]["verification_result"]["errors"]
    assert any(e.startswith("forbidden_field_value:branch=") for e in errors)


@pytest.mark.parametrize("protected_branch", ["master", "prod", "production"])
def test_git_commit_other_protected_branches_fail(tmp_path, protected_branch):
    def _h(args):
        return {
            "ok": True, "exit_code": 0,
            "branch": protected_branch,
            "before_head": "0" * 40, "after_head": "b" * 40, "commit_hash": "b" * 40,
        }

    reg = _registry_with("Bash", _h, workspace_root=tmp_path)
    (terminal, *_), _r = _run_and_gate(
        reg, "Bash", {"command": "git commit", "command_kind": "git_commit"}
    )
    assert terminal == "failed"


def test_git_commit_before_equals_after_without_reason_fails(tmp_path):
    """No reason="head_unchanged" — but cross_field_inequality catches it."""

    def _h(args):
        return {
            "ok": True, "exit_code": 0,
            "branch": "feat/x",
            "before_head": "a" * 40, "after_head": "a" * 40, "commit_hash": "a" * 40,
        }

    reg = _registry_with("Bash", _h, workspace_root=tmp_path)
    (terminal, _v, new_ck, _ev), _r = _run_and_gate(
        reg, "Bash", {"command": "git commit", "command_kind": "git_commit"}
    )
    assert terminal == "failed"
    errors = new_ck["promote_gate_envelope"]["verification_result"]["errors"]
    assert "cross_field_inequality_violated:before_head!=after_head" in errors


def test_git_commit_after_head_diverges_from_commit_hash_fails(tmp_path):
    def _h(args):
        return {
            "ok": True, "exit_code": 0,
            "branch": "feat/x",
            "before_head": "0" * 40, "after_head": "a" * 40, "commit_hash": "b" * 40,
        }

    reg = _registry_with("Bash", _h, workspace_root=tmp_path)
    (terminal, _v, new_ck, _ev), _r = _run_and_gate(
        reg, "Bash", {"command": "git commit", "command_kind": "git_commit"}
    )
    assert terminal == "failed"
    errors = new_ck["promote_gate_envelope"]["verification_result"]["errors"]
    assert "cross_field_equality_violated:after_head=commit_hash" in errors


def test_git_commit_happy_path_still_passes(tmp_path):
    def _h(args):
        return {
            "ok": True, "exit_code": 0,
            "branch": "feat/clean",
            "before_head": "0" * 40, "after_head": "c" * 40, "commit_hash": "c" * 40,
        }

    reg = _registry_with("Bash", _h, workspace_root=tmp_path)
    (terminal, verification, _nc, _ev), _r = _run_and_gate(
        reg, "Bash", {"command": "git commit", "command_kind": "git_commit"}
    )
    assert terminal == "succeeded"
    assert verification == "passed"


# ===========================================================================
# C. AnalyzeImage — must_be_nonempty_str
# ===========================================================================


def test_analyze_image_empty_description_fails(tmp_path):
    def _h(args):
        return {"ok": True, "description": "", "model_used": "stub"}

    reg = _registry_with("AnalyzeImage", _h, workspace_root=tmp_path)
    (terminal, _v, new_ck, _ev), _r = _run_and_gate(reg, "AnalyzeImage", {"image_path": "/tmp/x"})
    assert terminal == "failed"
    assert "must_be_nonempty_str_violated:description" in new_ck["promote_gate_envelope"]["verification_result"]["errors"]


def test_analyze_image_whitespace_only_description_fails(tmp_path):
    def _h(args):
        return {"ok": True, "description": "   \n\t  ", "model_used": "stub"}

    reg = _registry_with("AnalyzeImage", _h, workspace_root=tmp_path)
    (terminal, _v, new_ck, _ev), _r = _run_and_gate(reg, "AnalyzeImage", {"image_path": "/tmp/x"})
    assert terminal == "failed"
    assert "must_be_nonempty_str_violated:description" in new_ck["promote_gate_envelope"]["verification_result"]["errors"]


def test_analyze_image_empty_model_used_fails(tmp_path):
    def _h(args):
        return {"ok": True, "description": "ok", "model_used": ""}

    reg = _registry_with("AnalyzeImage", _h, workspace_root=tmp_path)
    (terminal, _v, new_ck, _ev), _r = _run_and_gate(reg, "AnalyzeImage", {"image_path": "/tmp/x"})
    assert terminal == "failed"
    assert "must_be_nonempty_str_violated:model_used" in new_ck["promote_gate_envelope"]["verification_result"]["errors"]


def test_analyze_image_valid_output_still_passes(tmp_path):
    def _h(args):
        return {"ok": True, "description": "A photo of a cat.", "model_used": "gpt-vision-stub"}

    reg = _registry_with("AnalyzeImage", _h, workspace_root=tmp_path)
    (terminal, verification, _nc, _ev), _r = _run_and_gate(reg, "AnalyzeImage", {"image_path": "/tmp/x"})
    assert terminal == "succeeded"
    assert verification == "passed"


def test_analyze_image_still_redacts_image_bytes(tmp_path):
    """Redaction stays intact under F3a-ext.1."""
    blob = "BASE64SECRETBLOB-INVISIBLE-IN-LEDGER"

    def _h(args):
        return {"ok": True, "description": "ok", "model_used": "stub"}

    reg = _registry_with("AnalyzeImage", _h, workspace_root=tmp_path)
    result = reg.execute(
        "AnalyzeImage",
        {"image_path": "/tmp/x", "image_b64": blob, "image_bytes": "FF" * 100, "image_data": "more"},
        agent_class="operator",
    )
    artifact = result[ARTIFACT_RESULT_KEY]
    assert blob not in str(artifact)
    redacted_keys = set(artifact["tool_args_redacted"].keys())
    assert {"image_b64", "image_bytes", "image_data"}.isdisjoint(redacted_keys)


# ===========================================================================
# D. Path root constraint sanity (must_be_existing_path)
# ===========================================================================


def test_must_be_existing_path_rejects_outside_root(tmp_path):
    """A SkillGenerate result whose path is real but lives OUTSIDE the
    workspace and OUTSIDE /tmp must be rejected by must_be_existing_path."""
    outside = Path("/private/etc/hosts")
    if not outside.exists():
        pytest.skip("test depends on /private/etc/hosts")

    body = outside.read_bytes()
    real_sha = hashlib.sha256(body).hexdigest()

    def _h(args):
        return {
            "ok": True, "name": "x",
            "path": str(outside),
            "size_bytes": outside.stat().st_size,
            "sha256_hash": real_sha,
        }

    reg = _registry_with("SkillGenerate", _h, workspace_root=tmp_path)
    (terminal, _v, new_ck, _ev), _r = _run_and_gate(reg, "SkillGenerate", {"task": "x"})
    assert terminal == "failed"
    errors = new_ck["promote_gate_envelope"]["verification_result"]["errors"]
    assert any("outside_allowed_root" in e for e in errors)
