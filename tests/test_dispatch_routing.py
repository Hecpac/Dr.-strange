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
from claw_v2.semantic_turn import SemanticTurn
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
    classified = bot._classify_task_intent(
        "estado de la task nlm-5a9c55c8929d", session_id="t"
    )
    assert classified.get("intent") in {
        "status_question",
        "resume_previous",
    }, classified


# ---------------------------------------------------------------------------
# Commit #4 — explicit dispatch_decision telemetry
# ---------------------------------------------------------------------------


def _capture_dispatch_decisions(bot, text: str) -> list[dict]:
    """Drive `handle_text` and return the ordered list of dispatch_decision
    payloads emitted while processing the message. Other observe events are
    ignored so the assertion focuses on the routing chain."""
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


def test_dispatch_decision_chain_for_ambiguous_prompts(bot) -> None:
    """For each of the four ambiguous Spanish prompts the pre-brain handler
    chain must emit `dispatch_decision` rows whose `route` is `fall_through`
    (with appropriate reasons), proving none of them got intercepted."""
    for prompt in AMBIGUOUS_MESSAGES:
        events = _capture_dispatch_decisions(bot, prompt)
        assert events, f"no dispatch_decision events emitted for {prompt!r}"

        # Schema sanity: each event has the required fields and the preview
        # never exposes more than 80 chars of the user's message.
        for ev in events:
            assert ev.get("handler"), f"missing handler in {ev!r}"
            assert ev.get("route") in {
                "intercepted",
                "fall_through",
                "explicit_command",
            }, ev
            assert ev.get("reason"), f"missing reason in {ev!r}"
            assert ev.get("text_len") == len(prompt)
            assert len(ev.get("text_preview") or "") <= 80

        # No explicit_command marker for ambiguous chat.
        assert not any(ev["route"] == "explicit_command" for ev in events), (
            f"ambiguous prompt produced explicit_command marker: {prompt!r}"
        )

        handlers_seen = [ev["handler"] for ev in events]
        # The four key pre-brain handlers must all weigh in.
        for required in (
            "operational_alert",
            "task_intent",
            "operational_status",
            "nlm_natural_language",
        ):
            assert required in handlers_seen, (
                f"{required} did not emit a dispatch_decision for {prompt!r}; "
                f"chain={handlers_seen}"
            )

        # task_intent must fall through with the disabled_by_flag reason
        # (default production setting), proving the brain-bypass guard fired.
        task_intent_event = next(
            ev for ev in events if ev["handler"] == "task_intent"
        )
        assert task_intent_event["route"] == "fall_through", task_intent_event
        assert task_intent_event["reason"] in {
            "disabled_by_flag",
            "task_intent_no_match",
        }, task_intent_event


def test_dispatch_decision_marks_explicit_task_id(bot) -> None:
    """Messages naming a literal task_id must produce an `explicit_command`
    marker event so the audit stream can separate intentional commands from
    heuristic captures."""
    events = _capture_dispatch_decisions(
        bot, "estado de la task nlm-5a9c55c8929d"
    )
    assert any(
        ev["route"] == "explicit_command"
        and ev["reason"] == "literal_task_id_match"
        for ev in events
    ), f"missing explicit_command marker for task_id message; chain={events!r}"


def test_dispatch_decision_payload_includes_matched_pattern_field(bot) -> None:
    """Wave 2.4: every dispatch_decision payload carries a `matched_pattern`
    field. For captured events it is at least the handler name; richer
    labels (`shortcut.url_extract`, `task_intent.resume_previous_es`) are
    populated by handlers that expose their classification. Lets `claw think
    tail --type dispatch_decision` show *why* a handler fired, not just *which*."""
    events = _capture_dispatch_decisions(bot, "estado de la task nlm-5a9c55c8929d")
    assert events, "expected at least one dispatch_decision event"
    for ev in events:
        assert "matched_pattern" in ev, f"matched_pattern missing from payload: {ev!r}"
    captured = [ev for ev in events if ev.get("captured")]
    if captured:
        for ev in captured:
            assert ev["matched_pattern"], (
                f"captured event must have non-empty matched_pattern: {ev!r}"
            )


class _CaptureObserve:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event_type: str, *, payload: dict | None = None, **_kwargs) -> None:
        self.events.append({"event_type": event_type, "payload": payload or {}})


def _jwt_that_would_leak_if_truncated_first() -> str:
    return "eyJhbGciOiJI" + ("a" * 30) + ".eyJzdWIi" + ("b" * 30) + ".sig"


def test_dispatch_decision_redacts_text_preview_before_truncating(bot) -> None:
    capture = _CaptureObserve()
    bot.observe = capture
    token = _jwt_that_would_leak_if_truncated_first()

    bot._emit_dispatch_decision(
        session_id="s-redact",
        text=f"token {token}",
        handler="test_handler",
        captured=False,
        route="fall_through",
        reason="test",
    )

    preview = capture.events[-1]["payload"]["text_preview"]
    assert preview == "token [REDACTED]"
    assert "eyJ" not in preview


def test_semantic_turn_trace_redacts_text_preview_before_truncating(bot) -> None:
    capture = _CaptureObserve()
    bot.observe = capture
    token = _jwt_that_would_leak_if_truncated_first()

    bot._emit_semantic_turn_trace(
        session_id="s-redact",
        text=f"token {token}",
        semantic_turn=SemanticTurn(
            intent="question",
            objective=None,
            confidence=0.9,
            clear_goal=False,
            explicit_authorization=False,
            explicit_continuation=False,
            debug_mode=False,
            reasons=("test",),
        ),
        state_sources_checked=[],
        approval_scope_match="none",
        decision="fallthrough",
        output_kind="brain",
    )

    preview = capture.events[-1]["payload"]["text_preview"]
    assert preview == "token [REDACTED]"
    assert "eyJ" not in preview
