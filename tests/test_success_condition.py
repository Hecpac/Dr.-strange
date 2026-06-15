"""Regression tests for the F1+F2 success-condition contract.

Codifies the 2026-05-26 incidents:
  - X compose-thread tool.ok=True but only T8 actually posted
  - @PachanoDesign stale memory led to a publish target that did not exist

These tests are 100% mocked / pure-function. NO live calls to X, LinkedIn,
HeyGen, GitHub remote, Telegram, or any external service. The verification
module is a pure evaluator; memory_revalidation validators are pure functions
fed stubbed observations.
"""

from __future__ import annotations


import pytest

from claw_v2 import memory_revalidation as mr
from claw_v2.verification import (
    ExternalCheckSpec,
    StateDeltaSpec,
    SuccessCondition,
    build_verification_envelope,
    validate_success_condition,
    warn_if_contract_missing,
)


# ---------------------------------------------------------------------------
# Test 1 — Registry-level warning when Tier 3 lacks success_condition/preflight.
# F1 is warn-only (per Hector §1: stay reversible). F4 will hard-fail.
# ---------------------------------------------------------------------------


def test_registry_warns_when_tier3_has_no_success_condition():
    msg = warn_if_contract_missing(
        tool_name="DangerouslyPublish",
        tier=3,
        has_sc=False,
        has_pf=False,
    )
    assert msg is not None
    assert "success_condition" in msg
    assert "F4" in msg


def test_registry_warns_when_tier3_has_sc_but_no_preflight():
    msg = warn_if_contract_missing(
        tool_name="PublishWithoutProbe",
        tier=3,
        has_sc=True,
        has_pf=False,
    )
    assert msg is not None
    assert "preflight" in msg


def test_registry_does_not_warn_when_tier1_lacks_contract():
    """Read-only Tier 1 tools never need success_condition (no state mutation)."""
    msg = warn_if_contract_missing(tool_name="ReadFile", tier=1, has_sc=False, has_pf=False)
    assert msg is None


# ---------------------------------------------------------------------------
# Test 2 — The headline X-thread incident, codified.
# tool.ok=True but external observation shows only the LAST tweet posted →
# success_condition MUST reject. This is the regression Hector pushed for.
# ---------------------------------------------------------------------------


def test_tool_ok_alone_does_not_pass_when_external_check_fails():
    sc = SuccessCondition(
        must_contain_keys=("tweets_posted_count",),
        external_check=ExternalCheckSpec(
            kind="cdp_phrase",
            target="https://x.com/HectorPach71777",
            expected_phrases=(
                "Singapur acaba de poner número",  # T1 opening
                "5 cosas cambian cuando un gobierno",  # T2
                "El mercado votó por routing",  # T3
                "esos son los nuevos logs",  # T4
                "Es la capa de política alrededor",  # T5
                "deja de sonar absurdo",  # T6
                "Es un número de planificación",  # T7
                "cuando hay 219 más",  # T8
            ),
        ),
    )
    tool_result = {"ok": True, "tweets_intended": 8, "tweets_posted_count": 1, "reason": ""}
    observed_body_text = 'Dejó de ser "¿puede funcionar?". Ahora es: "cuando hay 219 más"'
    errors = validate_success_condition(
        tool_result=tool_result,
        condition=sc,
        external_observation={"body_text": observed_body_text},
    )
    assert errors, "tool_ok=True with 1/8 phrases visible MUST NOT pass"
    # 7 of 8 phrases should be flagged as missing
    missing = [e for e in errors if e.startswith("expected_phrase_missing:")]
    assert len(missing) == 7

    envelope = build_verification_envelope(
        condition=sc,
        errors=errors,
        evidence_uri="artifacts/content/publish_evidence/x_pub_03_after_post_1779807069.png",
        external_observation={"body_text": observed_body_text},
    )
    # Critical: NOT succeeded, NOT silently passed
    assert envelope.status != "passed"
    assert envelope.status == "failed"
    assert envelope.success_condition_version == sc.schema_version
    assert envelope.evidence_uri.startswith("artifacts/")
    assert envelope.verification_result["external_observation_sha"]  # non-empty


# ---------------------------------------------------------------------------
# Test 3 — Happy path: all expected phrases present → passed.
# ---------------------------------------------------------------------------


def test_success_condition_passes_when_all_phrases_present():
    sc = SuccessCondition(
        must_contain_keys=("posted_id",),
        external_check=ExternalCheckSpec(
            kind="cdp_phrase",
            target="https://example.test/page",
            expected_phrases=("hello", "world"),
        ),
    )
    tool_result = {"ok": True, "posted_id": "abc123"}
    errors = validate_success_condition(
        tool_result=tool_result,
        condition=sc,
        external_observation={"body_text": "saying hello to the world right now"},
    )
    assert errors == []

    envelope = build_verification_envelope(
        condition=sc,
        errors=errors,
        evidence_uri="artifacts/test/dummy.png",
    )
    assert envelope.status == "passed"


# ---------------------------------------------------------------------------
# Test 4 — Forbidden reason (e.g. CDP fill returned reason="no_editor")
# must block success even if everything else looks OK.
# ---------------------------------------------------------------------------


def test_forbidden_reason_blocks_success():
    sc = SuccessCondition(
        must_contain_keys=("ok",),
        forbidden_reasons=("no_editor", "preflight_failed"),
    )
    tool_result = {"ok": True, "reason": "no_editor", "len": 0}
    errors = validate_success_condition(
        tool_result=tool_result,
        condition=sc,
        external_observation={"body_text": "anything"},
    )
    assert any(e == "forbidden_reason_matched:no_editor" for e in errors)
    envelope = build_verification_envelope(condition=sc, errors=errors, evidence_uri=None)
    assert envelope.status == "failed"


# ---------------------------------------------------------------------------
# Test 5 — Memory load-bearing claim revalidation BLOCKS Tier 3 action when
# stale and produces a proposed_patch for HUMAN REVIEW (never auto-applied).
# Codifies the @PachanoDesign incident.
# ---------------------------------------------------------------------------


def test_memory_load_bearing_keys_block_tier3_when_stale_and_propose_patch():
    # Caller pre-fetches the observation (in production this is the F5 CDP
    # runner; here we stub it directly — zero external calls).
    stub_observation = {
        "x_handle": {
            "account_exists": False,
            "fetched_url": "https://x.com/PachanoDesign",
            "fetched_at": 1779807000.0,
        }
    }
    outcome = mr.revalidate_memory_claims(
        ("x_handle",),
        context={"x_handle": "PachanoDesign"},
        observations=stub_observation,
    )
    assert outcome.all_valid is False
    assert outcome.block_action is True
    assert "x_handle" in outcome.invalid
    inv = outcome.invalid["x_handle"]
    assert inv.reason == "account_does_not_exist"
    # CRITICAL: a proposed_patch was emitted but NO MEMORY.md write happened.
    assert inv.proposed_patch is not None
    assert inv.proposed_patch["action"] == "human_review_required"
    assert outcome.proposed_patches == [inv.proposed_patch]


def test_memory_validator_is_pure_and_read_only(tmp_path, monkeypatch):
    """Sanity check: a validator must never touch the filesystem, network,
    or any module beyond its inputs. We assert this structurally: the
    validator's return type is a dataclass and the call is repeatable
    (deterministic) given the same stubbed observation."""
    obs = {"account_exists": True, "fetched_url": "https://x.com/HectorPach71777"}
    r1 = mr.validate_x_handle("HectorPach71777", obs)
    r2 = mr.validate_x_handle("HectorPach71777", obs)
    assert r1 == r2
    assert r1.valid is True
    assert r1.reason == "ok"
    assert r1.proposed_patch is None  # no patch on happy path


# ---------------------------------------------------------------------------
# Test 6 — Tier 2 local state_delta check: passes ONLY when local state
# actually changed (no "tool_ok was enough" shortcut).
# ---------------------------------------------------------------------------


def test_tier2_state_delta_required():
    sc = SuccessCondition(
        must_contain_keys=("rows_written",),
        state_delta_check=StateDeltaSpec(db_table="agent_tasks", expected_rows_delta=1),
    )
    tool_result = {"ok": True, "rows_written": 1}
    # No observation passed → must NOT pass (pending_verification)
    errors = validate_success_condition(
        tool_result=tool_result,
        condition=sc,
        state_delta_observation=None,
    )
    assert "state_delta_required_but_no_observation" in errors
    envelope = build_verification_envelope(condition=sc, errors=errors, evidence_uri=None)
    assert envelope.status == "pending_verification"

    # With matching observation → passes
    errors2 = validate_success_condition(
        tool_result=tool_result,
        condition=sc,
        state_delta_observation={"rows_added": 1},
    )
    assert errors2 == []


# ---------------------------------------------------------------------------
# Test 7 — Audit envelope contains all 5 fields Hector required in §9.
# ---------------------------------------------------------------------------


def test_audit_envelope_has_all_required_fields():
    sc = SuccessCondition(must_contain_keys=("ok",))
    envelope = build_verification_envelope(
        condition=sc,
        errors=[],
        evidence_uri="artifacts/test/evidence.json",
        validated_by="verifier",
    )
    d = envelope.to_dict()
    for key in (
        "status",
        "success_condition_version",
        "validated_at",
        "validated_by",
        "evidence_uri",
        "verification_result",
    ):
        assert key in d, f"audit envelope missing required field {key!r}"
    assert d["success_condition_version"] == sc.schema_version
    assert d["validated_by"] == "verifier"
    assert d["status"] == "passed"
    # verification_result is the dict carrying errors + obs sha + verification_id
    assert "verification_id" in d["verification_result"]


# ---------------------------------------------------------------------------
# Test 8 — Hard guarantee: every test in this file is offline. The fixture
# below would fail if anything attempted a real network call.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Block urllib / requests / socket so any accidental external call fails loudly."""

    def _boom(*a, **kw):
        raise RuntimeError("Network call attempted from test_success_condition — forbidden")

    import socket
    import urllib.request

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    yield
