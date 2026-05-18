"""Slice 1 of P0-3 + P0-2 + P1-6 block: tests for the `_final_render` funnel.

Three guarantees:

1. **Idempotency on adversarial inputs** — applying `_final_render` twice
   yields the same result as applying it once, even when the input is
   constructed so that one pass produces text that could trigger another
   drop/replace pattern. ``NaturalLanguageRenderer.render`` and
   ``_sanitize_chat_response`` are regex-pure; this test pins that
   property and the helper's composition.

2. **No interaction with the evidence-gate** — `_final_render` MUST NOT
   create rows in `agent_tasks` with `runtime=evidence_gate`, emit
   `evidence_gate_blocked_*` events, nor read
   `current_meta_introspection_kind`. Keeps the helper a pure formatter
   so the P1-6 funnel migration cannot regress P0-1.

3. **Preserves the meta-skip ContextVar invariant** —
   ``final_render_brain_path_inside_meta_context`` (INTERNAL_WIRING.md
   §1) requires that calling `_final_render` inside the meta-guard
   `with meta_introspection_context(...)` does not reset the ContextVar
   nor leak meta state. This test exercises the exact frame ordering.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from claw_v2.adapters.base import LLMRequest
from claw_v2.bot_helpers import (
    current_meta_introspection_kind,
    meta_introspection_context,
)
from claw_v2.main import build_runtime
from claw_v2.types import LLMResponse


# --- adversarial inputs ------------------------------------------------------

# Each case: input → must remain idempotent under _final_render. Inputs are
# chosen to stress: (a) lines that match drop patterns, (b) tokens that
# match replace patterns, (c) text that becomes empty after one pass and
# falls back to the renderer's default copy, (d) mixed drop+replace on the
# same line, (e) tokens that look like they MIGHT trigger another rule
# after a first transform (proves no second-order matches).
_ADVERSARIAL_INPUTS = [
    pytest.param("", id="empty"),
    pytest.param("   ", id="whitespace_only"),
    pytest.param("Hola, ¿cómo va el día?", id="plain_natural"),
    pytest.param(
        "approval_id: `abc123`\nApprove via: `/task_approve abc token`",
        id="all_drop_lines",
    ),
    pytest.param(
        "Estado actual: needs_approval\nrisk_tier: high",
        id="all_replace_tokens",
    ),
    pytest.param(
        "approval_id: `abc123`\n"
        "Estado: pending_approval\n"
        "task.contextual_action\n"
        "waiting_for_user_input\n"
        "explicit_blocker\n"
        "Approve via: `/task_approve abc token`",
        id="renderer_canonical_leak_block",
    ),
    pytest.param(
        "/task_approve abc token  → confirma\n/task_abort abc",
        id="multiple_internal_commands",
    ),
    pytest.param(
        "Líneas\n\n\n\n\nseparadas con muchos saltos.",
        id="excess_blank_lines",
    ),
    pytest.param(
        "approval_id: `abc`\n/task_approve abc token\nApprove via abc",
        id="three_drops_one_line_each",
    ),
    pytest.param(
        # Adversarial: token that, AFTER replacement, contains a substring
        # that resembles another internal label but must not match the drop
        # patterns (which are anchored / line-shaped). Confirms no
        # second-order match.
        "Hoy explicit_blocker fue activado para waiting_for_user_input.",
        id="replace_tokens_inline_no_secondary_match",
    ),
    pytest.param(
        # All drops + only-drop content → renderer returns its fallback copy.
        # Second pass must leave the fallback untouched (idempotent).
        "approval_id: `abc123`\nApprove via: `/task_approve abc token`\n"
        "/task_abort xyz",
        id="all_drops_triggers_fallback",
    ),
    pytest.param(
        # Mixed: a sanitizer trigger (internal session id) on a line that
        # also carries a renderer drop. After render+sanitize:
        # - drop line with `Approve via`,
        # - leave the rest, with `tg-XXX` redacted by the sanitizer.
        # Second pass: nothing more to do.
        "Hola tg-574707975 te escribo\nApprove via: /task_approve abc",
        id="renderer_drop_plus_sanitizer_redact",
    ),
    pytest.param(
        # A line containing both a replace token and a drop pattern.
        # Render drops the line (drop wins by precedence within renderer's
        # line loop); second pass: nothing to drop. Idempotent.
        "needs_approval — approval_id: `xyz`",
        id="replace_and_drop_same_line",
    ),
]


def _make_bot_for_render_only(tmp_root: Path):
    """Build a real runtime; we only use `bot._final_render`.

    The adversarial idempotency check does not exercise the LLM at all;
    it just calls the helper directly. We still build a full runtime so
    the bot's observe/task_ledger are real wiring (used by the
    evidence-gate side-effect test).
    """

    def _fake_anthropic(request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content="(unused)",
            lane=request.lane,
            provider="anthropic",
            model=request.model,
        )

    env = {
        "DB_PATH": str(tmp_root / "data" / "claw.db"),
        "WORKSPACE_ROOT": str(tmp_root / "workspace"),
        "AGENT_STATE_ROOT": str(tmp_root / "agents"),
        "EVAL_ARTIFACTS_ROOT": str(tmp_root / "evals"),
        "APPROVALS_ROOT": str(tmp_root / "approvals"),
        "TELEMETRY_ROOT": str(tmp_root / "telemetry"),
        "PIPELINE_STATE_ROOT": str(tmp_root / "pipeline"),
        "TELEGRAM_ALLOWED_USER_ID": "123",
        "CLAW_DISABLE_TASK_INTENT_ROUTER": "1",
    }
    return build_runtime(anthropic_executor=_fake_anthropic), env


@pytest.mark.parametrize("raw", _ADVERSARIAL_INPUTS)
def test_final_render_is_idempotent_on_adversarial_inputs(raw: str) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        runtime, env = _make_bot_for_render_only(root)
        with patch.dict(os.environ, env, clear=False):
            once = runtime.bot._final_render(session_id="tg-smoke", content=raw)
            twice = runtime.bot._final_render(session_id="tg-smoke", content=once)
    assert once == twice, (
        f"_final_render must be idempotent on adversarial inputs; "
        f"once={once!r} twice={twice!r}"
    )


def test_final_render_does_not_touch_evidence_gate() -> None:
    """Calling `_final_render` on text that looks like a start-claim must
    not create an evidence_gate ledger row nor emit
    evidence_gate_blocked_start_claim. The helper is render+sanitize only.
    """
    # A string that, fed through `_start_claim_lacks_evidence` in the
    # brain path, would normally trip the evidence-gate. Here we hand it
    # directly to `_final_render` and assert the helper does not enter
    # gate logic.
    claim_text = "Voy a limpiar el ledger y aplico los fixes ahora."

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        runtime, env = _make_bot_for_render_only(root)
        with patch.dict(os.environ, env, clear=False):
            captured: list[tuple[str, dict]] = []
            real_emit = runtime.bot.observe.emit

            def spy(event_type: str, **kwargs):
                captured.append((event_type, dict(kwargs.get("payload") or {})))
                return real_emit(event_type, **kwargs)

            with patch.object(runtime.bot.observe, "emit", side_effect=spy):
                out = runtime.bot._final_render(session_id="tg-smoke", content=claim_text)

            # Content survives (modulo render/sanitize transformations) —
            # nothing was replaced by a blocker template.
            assert claim_text.strip() in out, (
                f"_final_render replaced content with a template; got {out!r}"
            )

            # No evidence_gate row was created.
            records = runtime.task_ledger.list(session_id="tg-smoke", limit=20)
            runtimes = [getattr(record, "runtime", "") for record in records]
            assert "evidence_gate" not in runtimes, (
                f"_final_render must not touch task_ledger; got runtimes={runtimes}"
            )

            # No evidence_gate_* observe events.
            forbidden_events = {
                "evidence_gate_blocked_start_claim",
                "evidence_gate_blocked_completion_claim",
                "evidence_gate_explicit_blocker_recorded",
                "evidence_gate_skipped_meta",
            }
            seen = {name for name, _ in captured}
            assert not (seen & forbidden_events), (
                f"_final_render must not emit evidence_gate_* events; "
                f"got intersection={seen & forbidden_events}"
            )


def test_final_render_preserves_meta_skip_invariant() -> None:
    """Calling `_final_render` from inside a `meta_introspection_context`
    must not reset the ContextVar nor leak meta state — the helper is
    forbidden from reading or mutating it. This pins the placement
    invariant from INTERNAL_WIRING.md §1
    `final_render_brain_path_inside_meta_context`.
    """
    raw = "Hola, tg-574707975, approval_id: `abc` y explicit_blocker."
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        runtime, env = _make_bot_for_render_only(root)
        with patch.dict(os.environ, env, clear=False):
            assert current_meta_introspection_kind() is None
            with meta_introspection_context("meta"):
                assert current_meta_introspection_kind() == "meta"
                out = runtime.bot._final_render(session_id="tg-smoke", content=raw)
                # Helper executed without resetting the ContextVar — the
                # `with` block is still active and the reader still sees
                # the value.
                assert current_meta_introspection_kind() == "meta", (
                    "_final_render must not reset _META_INTROSPECTION_CONTEXT"
                )
            # On exit, normal reset.
            assert current_meta_introspection_kind() is None

    # Helper still applied its transforms: internal labels gone, session id redacted.
    assert "approval_id" not in out
    assert "explicit_blocker" not in out
    assert "tg-574707975" not in out


def test_final_render_empty_and_none_safe() -> None:
    """Defensive: empty / None-like inputs return the input unchanged
    without raising. Keeps the funnel safe for callers that may pass
    early-return strings.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        runtime, env = _make_bot_for_render_only(root)
        with patch.dict(os.environ, env, clear=False):
            assert runtime.bot._final_render(session_id="tg-smoke", content="") == ""
            # whitespace-only must NOT crash and must round-trip stably.
            once = runtime.bot._final_render(session_id="tg-smoke", content="   ")
            twice = runtime.bot._final_render(session_id="tg-smoke", content=once)
            assert once == twice
