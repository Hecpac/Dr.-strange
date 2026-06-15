"""Adversarial tests for the high-entropy redaction regex.

After 2026-05-11 hardening, the generic catcher (`_PATTERNS[1]`) requires
32+ chars and 8+ digits. Short tokens (e.g. 20-char Roboflow keys) are now
caught only by prefix-based patterns or field-name-based redaction.
"""

from __future__ import annotations

import time

import pytest

from claw_v2.redaction import redact_text


SHOULD_REDACT = [
    ("fake anthropic key body 36+ chars", "sk-ant-api03Abc123Xyz789Def456GhiJkl0987"),
    ("anthropic admin key fake", "AbcDefGhi123JklMnoPqr456StuVwxYza789ZzAa"),
    ("base64 jwt-style 40+ chars mixed", "eyJhbGciOiJIUzI1NiJ9Abc123Def456GhiJkl789Mno"),
    ("high entropy 40 chars", "AbcDef123GhiJkl456MnoPqr789StuVwx012YzAb"),
]


SHOULD_NOT_REDACT = [
    ("camelcase varname 24 chars", "myVeryLongVariableName123"),
    ("function name with digit", "getUserAccountById123"),
    ("git commit hash 40 hex lowercase", "eecd358abc1234567890def1234567890abc12d4"),
    ("sha256 lowercase hex 40 chars", "9ea02a6d43dc4ce7970b1c636c560904f62a799b"),
    ("uuid without hyphens lowercase", "f81d4fae7dec11d0a76500a0c91e6bf6"),
    ("uuid with hyphens lowercase", "81bbc5d7-3eb5-4cb0-8a93-b74aed8ce1af"),
    ("uuid hyphen mixed UPPER", "5d6282ce-54d0-4F5E-8bb7-2164436b375b"),
    ("parcel id short", "76092-0001"),
    ("roboflow model slug", "swimming-pools-dctlb"),
    ("branch name", "feat/tactical-autonomy-fixes"),
    ("telegram task id", "tg-574707975:1778533984299303000"),
    ("python class name", "MyVeryLongClassNameImpl"),
    (
        "ordinary english sentence",
        "We merged PR #2 with the Roboflow detector wired in cleanly today.",
    ),
    ("file path with hash", "/Users/hector/Projects/Dr.-strange/.git/objects/ee/cd358abc"),
    ("known gap: 20-char roboflow key (caught only with prefix context)", "8eyt8R1Hp008liTCA98a"),
    ("camelcase 32+ chars but only 1 digit", "thisIsAVeryLongCamelCaseIdentifier1"),
]


@pytest.mark.parametrize("name,specimen", SHOULD_REDACT)
def test_should_redact(name, specimen):
    out = redact_text(specimen, limit=0)
    assert "[REDACTED]" in out or "<REDACTED" in out, f"[{name}] expected redaction, got: {out!r}"


@pytest.mark.parametrize("name,specimen", SHOULD_NOT_REDACT)
def test_should_not_redact(name, specimen):
    out = redact_text(specimen, limit=0)
    assert "[REDACTED]" not in out and "<REDACTED" not in out, (
        f"[{name}] FALSE POSITIVE: input {specimen!r} got redacted to {out!r}"
    )


def test_no_catastrophic_backtracking_on_pathological_input():
    """1200-char patological input must process in well under 1s."""
    payload = "Aa1" * 400
    start = time.perf_counter()
    out = redact_text(payload, limit=0)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"regex took {elapsed:.2f}s on 1200-char pathological input"
    # 1200 chars with 400 digits — should match the hardened pattern.
    assert "[REDACTED]" in out


def test_telegram_task_id_passes_through_intact():
    s = "Investigating tg-574707975:1778533984299303000 timeout."
    out = redact_text(s, limit=0)
    assert "tg-574707975:1778533984299303000" in out, f"task id got mangled: {out!r}"


def test_prefix_based_redaction_still_catches_short_anthropic_key():
    """Short keys with prefix 'sk-' must still be redacted via _PATTERNS[2]."""
    s = "key=sk-ant-api03-XYZabc1234"
    out = redact_text(s, limit=0)
    assert "[REDACTED]" in out


def test_real_code_snippet_with_camelcase_is_intact():
    """A representative Python code snippet must round-trip without redaction."""
    code = (
        "class UserAccountRepository:\n"
        "    def getUserAccountById123(self, user_id):\n"
        "        return self.myVeryLongVariableName123\n"
    )
    out = redact_text(code, limit=0)
    assert "[REDACTED]" not in out, f"code snippet got mangled: {out!r}"
    assert "getUserAccountById123" in out
    assert "myVeryLongVariableName123" in out
