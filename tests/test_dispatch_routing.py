"""Dispatcher routing regression tests for the brain-bypass refactor.

Validates that ambiguous, conversational, and adversarial messages fall through
the pre-brain semantic routers (commit #1 / #2 of the refactor) instead of
being captured by the canned `task_intent` classifier.

Adversarial scenarios are derived from the four pushback patterns documented
in Anthropic's "How people ask Claude for personal guidance" (2026-04-30,
https://www.anthropic.com/research/claude-personal-guidance):

    1. Criticizing the assistant's initial assessment.
    2. Flooding with one-sided detail.
    3. Demanding binary verdicts on incomplete information.
    4. Asking the model to read intent into ordinary behavior.

The Anthropic paper measured pushback nearly doubling sycophancy rate (9% →
18%). The Claw analogue is a sycophant dispatcher: under conversational
pressure, the canned router should NOT capture the message and emit a stock
reply — it should fall through to the brain so the model can respond with
evidence.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from claw_v2.adapters.base import LLMRequest
from claw_v2.main import build_runtime
from claw_v2.types import LLMResponse


def _fake_anthropic(request: LLMRequest) -> LLMResponse:
    return LLMResponse(
        content="handled",
        lane=request.lane,
        provider="anthropic",
        model=request.model,
    )


@pytest.fixture
def bot():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        env = {
            "DB_PATH": str(root / "data" / "claw.db"),
            "WORKSPACE_ROOT": str(root / "workspace"),
            "AGENT_STATE_ROOT": str(root / "agents"),
            "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
            "APPROVALS_ROOT": str(root / "approvals"),
            "TELEMETRY_ROOT": str(root / "telemetry"),
            "PIPELINE_STATE_ROOT": str(root / "pipeline"),
            "TELEGRAM_ALLOWED_USER_ID": "123",
        }
        with patch.dict(os.environ, env, clear=False):
            runtime = build_runtime(anthropic_executor=_fake_anthropic)
            yield runtime.bot


AMBIGUOUS_MESSAGES = [
    "Porque te está costando terminar las tareas que te he pedido varias veces?",
    "Cuáles son las limitantes para que puedas abrir pestañas en la Mac, hacer click, escribir prompts y completar tareas?",
    "Eres claw o sigues siendo Claude code?",
    "Qué capacidades tienes para completar tareas autónomas?",
]


PUSHBACK_CRITICIZE_INITIAL = [
    "Tu respuesta anterior estuvo mal, las tareas no funcionan así.",
    "Estás equivocado en que la task quedó cerrada, revísalo otra vez.",
]


PUSHBACK_ONE_SIDED_FLOOD = [
    (
        "Llevo todo el día pidiéndote que cierres la task y no lo haces, "
        "siempre fallas, nunca completas, siempre te trabas, ¿por qué eres así?"
    ),
    (
        "Te di tres oportunidades, las tres fallaste, nunca terminas nada, "
        "no entiendes mis instrucciones, no sirves para tareas reales."
    ),
]


PUSHBACK_BINARY_VERDICT = [
    "¿La última task quedó completada o no? Sí o no.",
    "¿Estás roto definitivamente? Respuesta corta.",
]


PUSHBACK_INTENT_READING = [
    "¿Sientes que te están saboteando con tantos restarts?",
    "¿Crees que el sistema te está castigando con estos hotfixes?",
]


EXPLICIT_TASK_ID_MESSAGES = [
    "estado de la task nlm-5a9c55c8929d",
    "continúa task nlm-5a9c55c8929d",
    "reanuda la tarea tg-574707975:skill:1777581878766883000",
]


@pytest.mark.parametrize("text", AMBIGUOUS_MESSAGES)
def test_ambiguous_task_language_falls_through_to_brain(bot, text: str) -> None:
    """Generic 'why do you struggle / what can you do' questions must reach
    the brain — they are not actionable task commands."""
    response = bot._maybe_handle_task_intent(text, session_id="test-session")
    assert response is None, (
        f"Pre-brain task router captured ambiguous message: {text!r}\n"
        f"→ canned response: {response!r}"
    )


@pytest.mark.parametrize("text", AMBIGUOUS_MESSAGES)
def test_ambiguous_messages_classify_to_unknown_intent(bot, text: str) -> None:
    """Regression test: bypass the env flag and exercise the underlying
    classifier directly so ambiguous prompts cannot hit canned routes."""
    intent = bot._classify_task_intent(text, session_id="test-session")
    assert intent.get("intent") in {"unknown", "command"}, (
        f"Ambiguous message misclassified as canned route: {text!r} → {intent!r}"
    )


@pytest.mark.parametrize("text", PUSHBACK_CRITICIZE_INITIAL)
def test_pushback_criticize_does_not_trigger_canned(bot, text: str) -> None:
    """Pattern 1 from Anthropic sycophancy study: user criticizes the prior
    assessment. The dispatcher must not respond with a stock 'task closed'
    line — the brain should reassess against evidence."""
    response = bot._maybe_handle_task_intent(text, session_id="test-session")
    assert response is None


@pytest.mark.parametrize("text", PUSHBACK_ONE_SIDED_FLOOD)
def test_pushback_one_sided_flood_does_not_trigger_canned(bot, text: str) -> None:
    """Pattern 2: one-sided emotional flood. A sycophant dispatcher would
    accept the framing and emit 'sí, fallé'. Brain must judge with evidence."""
    response = bot._maybe_handle_task_intent(text, session_id="test-session")
    assert response is None


@pytest.mark.parametrize("text", PUSHBACK_BINARY_VERDICT)
def test_pushback_binary_verdict_does_not_trigger_canned(bot, text: str) -> None:
    """Pattern 3: demand for a binary verdict on incomplete information.
    The dispatcher must not commit the verifier to a stock yes/no — only the
    brain can check the ledger plus evidence."""
    response = bot._maybe_handle_task_intent(text, session_id="test-session")
    assert response is None


@pytest.mark.parametrize("text", PUSHBACK_INTENT_READING)
def test_pushback_intent_reading_does_not_trigger_canned(bot, text: str) -> None:
    """Pattern 4: asking the model to read sentiment / intent into ambient
    state. Must reach the brain; canned routes have no model of intent."""
    response = bot._maybe_handle_task_intent(text, session_id="test-session")
    assert response is None


@pytest.mark.parametrize("text", EXPLICIT_TASK_ID_MESSAGES)
def test_explicit_task_id_classification_recognizes_task(bot, text: str) -> None:
    """Counterpart: a message that names a literal task_id is the only kind
    of pre-brain task intent we want to allow. The classifier should produce
    a non-trivial intent (not 'unknown') even when the canned handler is off."""
    intent = bot._classify_task_intent(text, session_id="test-session")
    assert intent.get("intent") in {
        "status_question",
        "resume_previous",
        "command",
        "operational_alert",
    }, f"Explicit task-id message routed to unknown intent: {text!r} → {intent!r}"


# ---------------------------------------------------------------------------
# Commit #5 — make brain the default route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/status", True),
        ("/jobs", True),
        ("estado de la task nlm-5a9c55c8929d", True),
        ("continúa task tg-574707975:skill:1777581878766883000", True),
        ("Porque te está costando terminar las tareas?", False),
        ("Eres claw o sigues siendo Claude code?", False),
        ("hello world", False),
    ],
)
def test_is_explicit_command_classifier(text: str, expected: bool) -> None:
    """The brain-bypass refactor allows pre-brain routers to capture only
    explicit commands (`/foo`) or messages with a literal task_id. Anything
    else must fall through to the brain."""
    from claw_v2.bot import _is_explicit_command

    assert _is_explicit_command(text) is expected, f"_is_explicit_command misclassified {text!r}"


def test_semantic_prebrain_routes_default_off(bot, monkeypatch) -> None:
    """Default production behavior: heuristic semantic routers are off.
    Operators must opt in via CLAW_ENABLE_SEMANTIC_PREBRAIN_ROUTES=1."""
    monkeypatch.delenv("CLAW_ENABLE_SEMANTIC_PREBRAIN_ROUTES", raising=False)
    assert bot._semantic_prebrain_routes_enabled() is False
    monkeypatch.setenv("CLAW_ENABLE_SEMANTIC_PREBRAIN_ROUTES", "0")
    assert bot._semantic_prebrain_routes_enabled() is False
    monkeypatch.setenv("CLAW_ENABLE_SEMANTIC_PREBRAIN_ROUTES", "1")
    assert bot._semantic_prebrain_routes_enabled() is True


def test_literal_task_id_bypasses_disable_flag(bot, monkeypatch) -> None:
    """When the canned task router is disabled (default), an explicit
    literal task_id should still be eligible for routing through the
    classifier — the disable is a brain-bypass guard, not a hard kill."""
    # Disable flag stays at default ("1"); the carve-out should still let
    # this message pass through to the classifier, which then routes it.
    monkeypatch.setenv("CLAW_DISABLE_TASK_INTENT_ROUTER", "1")
    classified = bot._classify_task_intent("estado de la task nlm-5a9c55c8929d", session_id="t")
    assert classified.get("intent") in {
        "status_question",
        "resume_previous",
    }, classified


# ---------------------------------------------------------------------------
# Commit #4 — explicit dispatch_decision telemetry
# ---------------------------------------------------------------------------


def _capture_dispatch_decisions(bot, text: str) -> list[dict]:
    """Drive `handle_text` and return the list of dispatch_decision payloads
    emitted while processing the message.

    F0.3c consolidated dispatch telemetry: a single turn now emits exactly
    ONE ``dispatch_decision`` event whose ``tried_handlers[]`` array carries
    every handler considered. Returns those payloads (normally one)."""
    captured: list[dict] = []
    real_emit = bot.observe.emit

    def spy(event_type: str, **kwargs):
        if event_type == "dispatch_decision":
            captured.append(dict(kwargs.get("payload") or {}))
        return real_emit(event_type, **kwargs)

    with patch.object(bot.observe, "emit", side_effect=spy):
        try:
            bot.handle_text(user_id="123", session_id="dispatch-tel", text=text)
        except Exception:
            # The downstream brain stub or task ledger may raise on some
            # ambiguous prompts; we only care about the pre-brain emits.
            pass
    return captured


def _consolidated_decision(bot, text: str) -> dict:
    """Drive `handle_text` and return the single consolidated
    ``dispatch_decision`` payload for the turn (asserting exactly one)."""
    events = _capture_dispatch_decisions(bot, text)
    assert len(events) == 1, (
        f"expected exactly ONE consolidated dispatch_decision for {text!r}, "
        f"got {len(events)}: {events!r}"
    )
    return events[0]


def test_dispatch_decision_chain_for_ambiguous_prompts(bot) -> None:
    """For each ambiguous Spanish prompt the consolidated dispatch event must
    carry a ``tried_handlers[]`` array recording every pre-brain handler that
    weighed in — all falling through — proving none got intercepted."""
    for prompt in AMBIGUOUS_MESSAGES:
        decision = _consolidated_decision(bot, prompt)
        tried = decision.get("tried_handlers")
        assert isinstance(tried, list) and tried, (
            f"no tried_handlers recorded for {prompt!r}: {decision!r}"
        )

        # Schema sanity: each entry has the required fields; top-level preview
        # never exposes more than 80 chars of the user's message.
        for entry in tried:
            assert entry.get("handler"), f"missing handler in {entry!r}"
            assert entry.get("route") in {
                "intercepted",
                "fall_through",
                "brain_shortcut",
                "explicit_command",
            }, entry
            assert entry.get("reason"), f"missing reason in {entry!r}"
        assert decision.get("text_len") == len(prompt)
        assert len(decision.get("text_preview") or "") <= 80

        # Ambiguous chat falls through to the brain: nothing captured.
        assert decision.get("captured") is False, decision
        assert decision.get("selected_handler") is None, decision
        assert decision.get("selected_route") == "fall_through", decision
        assert not any(e["route"] == "explicit_command" for e in tried), (
            f"ambiguous prompt produced explicit_command marker: {prompt!r}"
        )

        handlers_seen = [e["handler"] for e in tried]
        # The four key pre-brain handlers must all weigh in.
        for required in (
            "operational_alert",
            "task_intent",
            "operational_status",
            "nlm_natural_language",
        ):
            assert required in handlers_seen, (
                f"{required} not in tried_handlers for {prompt!r}; chain={handlers_seen}"
            )

        # task_intent must fall through with the disabled_by_flag reason
        # (default production setting), proving the brain-bypass guard fired.
        task_intent_entry = next(e for e in tried if e["handler"] == "task_intent")
        assert task_intent_entry["route"] == "fall_through", task_intent_entry
        assert task_intent_entry["reason"] in {
            "disabled_by_flag",
            "task_intent_no_match",
        }, task_intent_entry


def test_dispatch_decision_marks_explicit_task_id(bot) -> None:
    """Messages naming a literal task_id must record an `explicit_command`
    entry in tried_handlers so the audit stream can separate intentional
    commands from heuristic captures."""
    decision = _consolidated_decision(bot, "estado de la task nlm-5a9c55c8929d")
    tried = decision.get("tried_handlers") or []
    assert any(
        e["route"] == "explicit_command" and e["reason"] == "literal_task_id_match" for e in tried
    ), f"missing explicit_command entry for task_id message; chain={tried!r}"


def test_explicit_command_marker_does_not_win_over_real_intercept(bot) -> None:
    """Review F0.3c blocker #1: the explicit_command MARKER (captured=True but
    not a real interception) must NOT be reported as the winner when a real
    handler intercepts the turn. selected_handler/route must be the real
    intercept; the marker stays in tried_handlers + the explicit_command flag."""
    decision = _consolidated_decision(bot, "estado de la task nlm-5a9c55c8929d")
    assert decision.get("captured") is True, decision
    assert decision.get("selected_route") != "explicit_command", decision
    assert decision.get("selected_handler") not in (None, "explicit_command"), decision
    assert decision.get("explicit_command") is True, decision  # marker preserved for audit
    tried = decision.get("tried_handlers") or []
    assert any(e["route"] == "explicit_command" for e in tried), decision


def test_unrecognized_slash_command_falls_through_not_captured(bot) -> None:
    """Review F0.3c blocker #2: an unrecognized slash command falls through to
    the brain; the explicit_command marker must NOT make the turn look
    captured (else the audit stream over-counts intercepts). captured=False,
    no winner, but the explicit_command flag + marker entry are preserved."""
    decision = _consolidated_decision(bot, "/zzznotacommand whatever")
    assert decision.get("captured") is False, decision
    assert decision.get("selected_handler") is None, decision
    assert decision.get("selected_route") == "fall_through", decision
    assert decision.get("explicit_command") is True, decision
    tried = decision.get("tried_handlers") or []
    assert any(e["route"] == "explicit_command" for e in tried), decision


def test_dispatch_decision_payload_includes_matched_pattern_field(bot) -> None:
    """Wave 2.4 (consolidated): every tried_handlers entry carries a
    `matched_pattern` field. For captured entries it is at least the handler
    name; richer labels are populated by handlers that expose their
    classification."""
    decision = _consolidated_decision(bot, "estado de la task nlm-5a9c55c8929d")
    tried = decision.get("tried_handlers") or []
    assert tried, "expected at least one tried_handlers entry"
    for entry in tried:
        assert "matched_pattern" in entry, f"matched_pattern missing: {entry!r}"
    captured = [e for e in tried if e.get("captured")]
    for entry in captured:
        assert entry["matched_pattern"], (
            f"captured entry must have non-empty matched_pattern: {entry!r}"
        )


# ---------------------------------------------------------------------------
# F0.3c — dispatch_decision consolidation (one event per turn)
# ---------------------------------------------------------------------------


def _count_dispatch_decisions(bot, text: str) -> int:
    """Drive `handle_text` and count how many ``dispatch_decision`` events
    were emitted for the turn (tripwire: must be exactly one)."""
    count = 0
    real_emit = bot.observe.emit

    def spy(event_type: str, **kwargs):
        nonlocal count
        if event_type == "dispatch_decision":
            count += 1
        return real_emit(event_type, **kwargs)

    with patch.object(bot.observe, "emit", side_effect=spy):
        try:
            bot.handle_text(user_id="123", session_id="dispatch-consol", text=text)
        except Exception:
            pass
    return count


def test_fall_through_turn_emits_exactly_one_dispatch_decision(bot) -> None:
    """TDD #1: an ambiguous turn that falls through to the brain emits ONE
    consolidated dispatch_decision (not one per pre-brain handler), and its
    tried_handlers array has multiple entries, all captured=False."""
    decision = _consolidated_decision(bot, AMBIGUOUS_MESSAGES[0])
    tried = decision.get("tried_handlers") or []
    assert len(tried) > 1, f"expected multiple tried handlers, got {tried!r}"
    assert all(e.get("captured") is False for e in tried), tried
    assert decision.get("selected_handler") is None
    assert decision.get("selected_route") == "fall_through"


def test_tried_handlers_records_all_considered_handlers(bot) -> None:
    """TDD #2: tried_handlers[] records every handler/route considered, each
    with its own reason and captured/status."""
    decision = _consolidated_decision(bot, AMBIGUOUS_MESSAGES[0])
    tried = decision.get("tried_handlers") or []
    handlers = [e["handler"] for e in tried]
    # Several distinct pre-brain handlers must appear, in chain order.
    assert "operational_alert" in handlers
    assert "operational_status" in handlers
    assert "nlm_natural_language" in handlers
    # Each entry is self-describing.
    for entry in tried:
        assert set(entry).issuperset({"handler", "route", "reason", "captured"}), entry


def test_captured_turn_emits_one_decision_with_selected_winner(bot) -> None:
    """TDD #3: a captured route still emits ONE dispatch_decision with
    selected_handler/selected_route set to the winner; tried_handlers
    includes the handlers tried before it."""
    decision = _consolidated_decision(bot, "estado de la task nlm-5a9c55c8929d")
    assert decision.get("captured") is True, decision
    assert decision.get("selected_handler"), decision
    # The winner is the REAL intercepting handler, never the explicit_command
    # marker (which is captured=True but not an actual interception).
    assert decision.get("selected_route") == "intercepted", decision
    assert decision["selected_handler"] != "explicit_command", decision
    tried = decision.get("tried_handlers") or []
    real_winners = [e for e in tried if e.get("captured") and e.get("route") != "explicit_command"]
    assert real_winners, f"no real (non-marker) captured entry: {tried!r}"
    assert decision["selected_handler"] == real_winners[0]["handler"], decision


def test_tripwire_no_turn_emits_more_than_one_dispatch_decision(bot) -> None:
    """TDD #4 (tripwire): neither a fall-through turn nor a captured turn may
    emit more than one dispatch_decision event."""
    assert _count_dispatch_decisions(bot, AMBIGUOUS_MESSAGES[0]) == 1
    assert _count_dispatch_decisions(bot, "estado de la task nlm-5a9c55c8929d") == 1


def test_consolidated_payload_preserves_audit_fields(bot) -> None:
    """TDD #5: the consolidated payload still carries the fields diagnostics /
    replay / think rely on (session_id, route, handler, reason, captured,
    text_preview, text_len) plus tried_handlers."""
    decision = _consolidated_decision(bot, "estado de la task nlm-5a9c55c8929d")
    for key in (
        "session_id",
        "route",
        "handler",
        "reason",
        "captured",
        "text_preview",
        "text_len",
        "tried_handlers",
    ):
        assert key in decision, f"missing audit field {key!r}: {decision!r}"
    # Back-compat: top-level handler/route mirror the selected winner.
    assert decision["handler"] == decision["selected_handler"]
    assert decision["route"] == decision["selected_route"]
