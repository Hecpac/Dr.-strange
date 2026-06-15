"""F3a-extension — SkillGenerate, AnalyzeImage contracts + Bash git_commit subcontract.

Offline only. tmp_path filesystem. NO live GPT vision / no real git / no
external network. Autouse `_no_network` fixture blocks sockets/urllib.
"""

from __future__ import annotations

import hashlib
import pytest

from claw_v2.tools import ToolDefinition, ToolRegistry
from claw_v2.verification.local_tool_contracts import (
    BASH_COMMAND_KIND_CONTRACTS,
    LOCAL_TOOL_SUCCESS_CONDITIONS,
)
from claw_v2.verification.local_tool_runner import (
    ARTIFACT_RESULT_KEY,
    CONTRACT_REQUIRED_KEY,
    lift_artifact_to_checkpoint,
)
from claw_v2.verification.promote_gate import apply_promote_gate_to_checkpoint


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("Network call attempted from extension test — forbidden")

    import socket
    import urllib.request

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    yield


def _registry_with(tool_name: str, handler):
    from pathlib import Path

    reg = ToolRegistry(workspace_root=Path("/tmp"))
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


def _checkpoint_with(artifact):
    return {
        "verification_status": "passed",
        "contract_required": True,
        "success_condition_artifact": artifact,
    }


# ===========================================================================
# SkillGenerate
# ===========================================================================


def test_skill_generate_contracts_registered():
    sc = LOCAL_TOOL_SUCCESS_CONDITIONS["SkillGenerate"]
    assert "path" in sc.must_contain_keys
    assert "sha256_hash" in sc.must_contain_keys
    assert "path" in sc.must_be_existing_path


def test_skill_generate_happy_path_with_file(tmp_path):
    out = tmp_path / "skills" / "my_skill.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = b"# Skill body\n"
    out.write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()  # full 64 hex (F3a-ext.1)

    def _handler(args):
        return {
            "ok": True,
            "name": "my_skill",
            "path": str(out),
            "size_bytes": len(payload),
            "sha256_hash": sha,
        }

    reg = _registry_with("SkillGenerate", _handler)
    result = reg.execute(
        "SkillGenerate",
        {"task": "do something useful", "tags": ["docs"]},
        agent_class="operator",
    )
    assert result[CONTRACT_REQUIRED_KEY] is True
    assert ARTIFACT_RESULT_KEY in result

    checkpoint = lift_artifact_to_checkpoint({"verification_status": "passed"}, result)
    terminal, verification, _new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "succeeded"
    assert verification == "passed"


def test_skill_generate_ok_true_but_no_file_fails(tmp_path):
    """Handler claims ok=True but the path does not exist → contract fails."""
    fake_path = tmp_path / "skills" / "missing.md"

    def _handler(args):
        return {
            "ok": True,
            "name": "missing_skill",
            "path": str(fake_path),
            "size_bytes": 42,
            "sha256_hash": "deadbeef" * 4,
        }

    reg = _registry_with("SkillGenerate", _handler)
    result = reg.execute(
        "SkillGenerate",
        {"task": "ghost skill"},
        agent_class="operator",
    )
    checkpoint = lift_artifact_to_checkpoint({"verification_status": "passed"}, result)
    terminal, verification, new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    assert verification == "failed"
    envelope = new_checkpoint["promote_gate_envelope"]
    assert "path_file_not_found:path" in envelope["verification_result"]["errors"]


def test_skill_generate_missing_required_key_fails(tmp_path):
    """Handler ok=True but missing sha256_hash → contract fails."""
    out = tmp_path / "skills" / "no_hash.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"content\n")

    def _handler(args):
        return {
            "ok": True,
            "name": "no_hash_skill",
            "path": str(out),
            "size_bytes": 8,
            # missing sha256_hash
        }

    reg = _registry_with("SkillGenerate", _handler)
    result = reg.execute("SkillGenerate", {"task": "x"}, agent_class="operator")
    checkpoint = lift_artifact_to_checkpoint({"verification_status": "passed"}, result)
    terminal, _v, new_checkpoint, _e = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    envelope = new_checkpoint["promote_gate_envelope"]
    assert "missing_key:sha256_hash" in envelope["verification_result"]["errors"]


def test_skill_generate_file_exists_but_empty_fails(tmp_path):
    """size_bytes>0 in result but actual file is 0 bytes on disk."""
    out = tmp_path / "skills" / "empty.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"")  # empty file

    def _handler(args):
        return {
            "ok": True,
            "name": "empty",
            "path": str(out),
            "size_bytes": 100,
            "sha256_hash": "abc1234567890def",
        }

    reg = _registry_with("SkillGenerate", _handler)
    result = reg.execute("SkillGenerate", {"task": "x"}, agent_class="operator")
    checkpoint = lift_artifact_to_checkpoint({"verification_status": "passed"}, result)
    terminal, _v, new_checkpoint, _e = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    envelope = new_checkpoint["promote_gate_envelope"]
    assert "path_file_empty:path" in envelope["verification_result"]["errors"]


def test_skill_generate_invalid_sha_fails(tmp_path):
    out = tmp_path / "skills" / "bad_sha.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"ok\n")

    def _handler(args):
        return {
            "ok": True,
            "name": "bad",
            "path": str(out),
            "size_bytes": 3,
            "sha256_hash": "NOT-HEX!!!",
        }

    reg = _registry_with("SkillGenerate", _handler)
    result = reg.execute("SkillGenerate", {"task": "x"}, agent_class="operator")
    checkpoint = lift_artifact_to_checkpoint({"verification_status": "passed"}, result)
    terminal, _v, new_checkpoint, _e = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    envelope = new_checkpoint["promote_gate_envelope"]
    assert "regex_mismatch:sha256_hash" in envelope["verification_result"]["errors"]


# ===========================================================================
# AnalyzeImage
# ===========================================================================


def test_analyze_image_contracts_registered():
    sc = LOCAL_TOOL_SUCCESS_CONDITIONS["AnalyzeImage"]
    assert "description" in sc.must_contain_keys
    assert "model_used" in sc.must_contain_keys


def test_analyze_image_happy_path():
    def _handler(args):
        return {
            "ok": True,
            "description": "A red apple on a wooden table.",
            "model_used": "gpt-vision-stub",
            "tokens_used": 42,
        }

    reg = _registry_with("AnalyzeImage", _handler)
    result = reg.execute(
        "AnalyzeImage",
        {"image_path": "/tmp/fake.png", "question": "what is this?"},
        agent_class="operator",
    )
    checkpoint = lift_artifact_to_checkpoint({"verification_status": "passed"}, result)
    terminal, verification, _new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "succeeded"
    assert verification == "passed"


def test_analyze_image_empty_output_fails():
    def _handler(args):
        return {"ok": True}  # missing description AND model_used

    reg = _registry_with("AnalyzeImage", _handler)
    result = reg.execute("AnalyzeImage", {"image_path": "/tmp/x.png"}, agent_class="operator")
    checkpoint = lift_artifact_to_checkpoint({"verification_status": "passed"}, result)
    terminal, _v, new_checkpoint, _e = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    envelope = new_checkpoint["promote_gate_envelope"]
    errors = envelope["verification_result"]["errors"]
    assert "missing_key:description" in errors
    assert "missing_key:model_used" in errors


def test_analyze_image_never_persists_image_bytes_in_artifact():
    """Bytes/base64 passed in tool_args MUST NOT leak into the artifact."""
    secret_blob = "BASE64BLOB-NOTGONNATOLERATEINLEDGER-AAAA"

    def _handler(args):
        return {"ok": True, "description": "ok", "model_used": "stub"}

    reg = _registry_with("AnalyzeImage", _handler)
    result = reg.execute(
        "AnalyzeImage",
        {"image_path": "/tmp/x.png", "image_b64": secret_blob, "image_bytes": "DEADBEEF"},
        agent_class="operator",
    )
    artifact = result[ARTIFACT_RESULT_KEY]
    artifact_str = str(artifact)
    assert secret_blob not in artifact_str
    assert "DEADBEEF" not in artifact_str
    # tool_args_redacted should NOT carry image_b64 / image_bytes / image_data
    redacted_keys = set(artifact["tool_args_redacted"].keys())
    assert "image_b64" not in redacted_keys
    assert "image_bytes" not in redacted_keys
    assert "image_data" not in redacted_keys


def test_analyze_image_forbidden_reason_blocks():
    def _handler(args):
        return {"ok": True, "description": "n/a", "model_used": "x", "reason": "invalid_image"}

    reg = _registry_with("AnalyzeImage", _handler)
    result = reg.execute("AnalyzeImage", {"image_path": "/tmp/x.png"}, agent_class="operator")
    checkpoint = lift_artifact_to_checkpoint({"verification_status": "passed"}, result)
    terminal, _v, new_checkpoint, _e = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    envelope = new_checkpoint["promote_gate_envelope"]
    assert any(
        "forbidden_reason_matched:invalid_image" == e
        for e in envelope["verification_result"]["errors"]
    )


# ===========================================================================
# Bash command_kind="git_commit" sub-contract
# ===========================================================================


def test_bash_git_commit_subcontract_registered():
    sub = BASH_COMMAND_KIND_CONTRACTS["git_commit"]
    assert "commit_hash" in sub.must_contain_keys
    assert "head_unchanged" in sub.forbidden_reasons
    assert "protected_branch_detected" in sub.forbidden_reasons
    assert "remote_push_attempted" in sub.forbidden_reasons


def test_bash_git_commit_happy_path():
    def _handler(args):
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": "[feat-xyz a1b2c3d] msg\n 1 file changed",
            "branch": "feat/xyz",
            "before_head": "0" * 40,
            "after_head": "a" * 40,
            "commit_hash": "a" * 40,
        }

    reg = _registry_with("Bash", _handler)
    result = reg.execute(
        "Bash",
        {"command": "git commit -m 'msg'", "command_kind": "git_commit"},
        agent_class="operator",
    )
    artifact = result[ARTIFACT_RESULT_KEY]
    # Merged contract is visible in the artifact
    assert "commit_hash" in artifact["success_condition"]["must_contain_keys"]
    assert "head_unchanged" in artifact["success_condition"]["forbidden_reasons"]

    checkpoint = lift_artifact_to_checkpoint({"verification_status": "passed"}, result)
    terminal, verification, _new_checkpoint, _events = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "succeeded"
    assert verification == "passed"


def test_bash_git_commit_head_unchanged_fails():
    def _handler(args):
        return {
            "ok": True,
            "exit_code": 0,
            "branch": "feat/xyz",
            "before_head": "a" * 40,
            "after_head": "a" * 40,
            "commit_hash": "a" * 40,
            "reason": "head_unchanged",
        }

    reg = _registry_with("Bash", _handler)
    result = reg.execute(
        "Bash",
        {"command": "git commit -m 'noop'", "command_kind": "git_commit"},
        agent_class="operator",
    )
    checkpoint = lift_artifact_to_checkpoint({"verification_status": "passed"}, result)
    terminal, _v, new_checkpoint, _e = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    envelope = new_checkpoint["promote_gate_envelope"]
    assert any(
        "forbidden_reason_matched:head_unchanged" == e
        for e in envelope["verification_result"]["errors"]
    )


def test_bash_git_commit_protected_branch_fails():
    def _handler(args):
        return {
            "ok": True,
            "exit_code": 0,
            "branch": "main",
            "before_head": "0" * 40,
            "after_head": "b" * 40,
            "commit_hash": "b" * 40,
            "reason": "protected_branch_detected",
        }

    reg = _registry_with("Bash", _handler)
    result = reg.execute(
        "Bash",
        {"command": "git commit -m 'on main'", "command_kind": "git_commit"},
        agent_class="operator",
    )
    checkpoint = lift_artifact_to_checkpoint({"verification_status": "passed"}, result)
    terminal, _v, new_checkpoint, _e = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    envelope = new_checkpoint["promote_gate_envelope"]
    assert any(
        "forbidden_reason_matched:protected_branch_detected" == e
        for e in envelope["verification_result"]["errors"]
    )


def test_bash_git_commit_invalid_hash_fails():
    def _handler(args):
        return {
            "ok": True,
            "exit_code": 0,
            "branch": "feat/x",
            "before_head": "0" * 40,
            "after_head": "not-a-real-sha",
            "commit_hash": "shortbad",
        }

    reg = _registry_with("Bash", _handler)
    result = reg.execute(
        "Bash",
        {"command": "git commit -m 'malformed'", "command_kind": "git_commit"},
        agent_class="operator",
    )
    checkpoint = lift_artifact_to_checkpoint({"verification_status": "passed"}, result)
    terminal, _v, new_checkpoint, _e = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "failed"
    envelope = new_checkpoint["promote_gate_envelope"]
    errors = envelope["verification_result"]["errors"]
    assert any(e.startswith("regex_mismatch:") for e in errors)


def test_bash_without_command_kind_uses_base_contract():
    """Plain bash without command_kind hint keeps the lighter base contract."""

    def _handler(args):
        return {"ok": True, "exit_code": 0, "stdout": "ok"}

    reg = _registry_with("Bash", _handler)
    result = reg.execute("Bash", {"command": "echo hi"}, agent_class="operator")
    artifact = result[ARTIFACT_RESULT_KEY]
    # Sub-contract keys must NOT be required for a plain Bash call.
    assert "commit_hash" not in artifact["success_condition"]["must_contain_keys"]

    checkpoint = lift_artifact_to_checkpoint({"verification_status": "passed"}, result)
    terminal, verification, _nc, _e = apply_promote_gate_to_checkpoint(
        raw_terminal_status="succeeded",
        raw_verification_status="passed",
        completed_checkpoint=checkpoint,
    )
    assert terminal == "succeeded"
    assert verification == "passed"
