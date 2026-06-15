from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from claw_v2.adapters.base import LLMRequest
from claw_v2.main import build_runtime
from claw_v2.natural_language_renderer import NaturalLanguageRenderer
from claw_v2.semantic_turn import classify_semantic_turn
from claw_v2.types import LLMResponse


def _fake_anthropic(request: LLMRequest) -> LLMResponse:
    return LLMResponse(
        content="BRAIN_FALLBACK_USED",
        lane=request.lane,
        provider="anthropic",
        model=request.model,
    )


def _runtime_env(root: Path) -> dict[str, str]:
    return {
        "DB_PATH": str(root / "data" / "claw.db"),
        "WORKSPACE_ROOT": str(root / "workspace"),
        "AGENT_STATE_ROOT": str(root / "agents"),
        "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
        "APPROVALS_ROOT": str(root / "approvals"),
        "TELEMETRY_ROOT": str(root / "telemetry"),
        "PIPELINE_STATE_ROOT": str(root / "pipeline"),
        "TELEGRAM_ALLOWED_USER_ID": "123",
        "CLAW_DISABLE_TASK_INTENT_ROUTER": "1",
    }


def _drive(
    bot,
    text: str,
    *,
    session_id: str = "tg-smoke",
    context_metadata: dict | None = None,
) -> tuple[str | None, list[dict]]:
    traces: list[dict] = []
    real_emit = bot.observe.emit

    def spy(event_type: str, **kwargs):
        if event_type == "semantic_turn_trace":
            traces.append(dict(kwargs.get("payload") or {}))
        return real_emit(event_type, **kwargs)

    with patch.object(bot.observe, "emit", side_effect=spy):
        response = bot.handle_text(
            user_id="123",
            session_id=session_id,
            text=text,
            runtime_channel="telegram",
            context_metadata=context_metadata,
        )
    return response, traces


def test_semantic_classifier_prioritizes_clear_new_task_over_audit_word() -> None:
    turn = classify_semantic_turn(
        "Crea una misión durable de prueba llamada audit-continuation-smoke..."
    )

    assert turn.intent == "new_task"
    assert turn.clear_goal is True
    assert turn.objective


def test_semantic_classifier_recognizes_operational_tasks_and_option_picks() -> None:
    task_samples = [
        "Crea un cuaderno y un podcasts sobre los agentes autonomos",
        "Crear el\nCuaderno",
        "Verifica que el daemon Levanto",
        "Verifica las cifras,y el asset visual generalo con ChatGPT image o nano banana",
        "Afínalo y que después cree las imágenes del grid",
        "Refina el carrusel y genera los assets visuales",
        "Haz un barrido por X de las noticias",
        "Haz un repaso por X",
        "Has un repaso por x",
        "Haz un repaso por X e investiga sobre la des habilitación de Fable 5",
        "Revisa las noticias el X",
        "Revisa las noticias desde chrome incluyendo X",
        "Abre Instagram",
        "Abre Instagram en Chrome",
        "Abre Instagram por defecto ya está loggeado",
        "Haz commit y reinicia",
        "Haz commit y reinicia el daemon",
        "Commitea y reinicia",
        "Reinicia el daemon",
        "Haz la auditoria de los MCps",
        "Revisa el repo de browser use",
        "Revisa el hilo https://x.com/openrouter/status/2065856853989270011?s=46",
        "https://x.com/openrouter/status/2065856853989270011?s=46 revisa el hilo",
    ]

    for text in task_samples:
        turn = classify_semantic_turn(text)
        assert turn.intent == "new_task", text
        assert turn.clear_goal is True
        assert turn.objective == text

    option_turn = classify_semantic_turn("Opción 1")
    assert option_turn.intent == "continue_active_mission"
    assert option_turn.explicit_continuation is True

    letter_turn = classify_semantic_turn("Opción A")
    assert letter_turn.intent == "continue_active_mission"
    assert letter_turn.explicit_continuation is True

    combo_turn = classify_semantic_turn("A y B")
    assert combo_turn.intent == "continue_active_mission"
    assert combo_turn.explicit_continuation is True

    plus_combo_turn = classify_semantic_turn("A+B")
    assert plus_combo_turn.intent == "continue_active_mission"
    assert plus_combo_turn.explicit_continuation is True

    spaced_combo_turn = classify_semantic_turn("A + B")
    assert spaced_combo_turn.intent == "continue_active_mission"
    assert spaced_combo_turn.explicit_continuation is True

    question_after_url = classify_semantic_turn("https://example.com/foo?x=1 que opinas")
    assert question_after_url.intent == "question"
    assert question_after_url.clear_goal is False

    contextual_action_samples = [
        "Publicalo",
        "Lee los docs",
        "Listo loggeado",
        "Arranca con el plan",
        "Okay 1",
        "Hazla Imagen en ChatGPT",
        "Ármalo",
        "Levántalo",
        "Mata y relanza",
        "Vuelve abrir chrome",
        "Vuelve a intentar abrir x",
        "Vuelve a intentarlo",
        "Tráela al frente",
        "Ya lo cerré",
        "Abrelo tu",
        "Ya esta desbloqueada",
        "Ya está desbloqueada",
    ]
    for text in contextual_action_samples:
        turn = classify_semantic_turn(text)
        assert turn.intent == "continue_active_mission", text
        assert turn.explicit_continuation is True

    stop_turn = classify_semantic_turn(
        "No continuemos porque ingles nativo es un problema si la entrevista es conversational"
    )
    assert stop_turn.intent == "correction_or_behavior_instruction"
    assert stop_turn.clear_goal is False


def test_natural_language_renderer_hides_internal_labels_in_normal_mode() -> None:
    raw = (
        "Intent: `ui.open_app`\n"
        "Target: `Claude`\n"
        "approval_id: `abc123`\n"
        "Estado: `pending_approval`\n"
        "Task: `tg-test:telegram-imperative:123`\n"
        "task.contextual_action\n"
        "waiting_for_user_input\n"
        "explicit_blocker\n"
        "Approve via: `/task_approve abc token`"
    )

    renderer = NaturalLanguageRenderer(mode="normal")
    rendered = renderer.render(raw)

    assert "approval_id" not in rendered
    assert "Intent:" not in rendered
    assert "Target:" not in rendered
    assert "Estado:" not in rendered
    assert "Task:" not in rendered
    assert "pending_approval" not in rendered
    assert "task.contextual_action" not in rendered
    assert "waiting_for_user_input" not in rendered
    assert "explicit_blocker" not in rendered
    assert "/task_approve" not in rendered
    assert renderer.leaked_internal_labels(raw)


def test_brain_first_new_task_ignores_unrelated_pending_approval_and_waits_for_procede() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = build_runtime(anthropic_executor=_fake_anthropic)
            runtime.bot.coordinator = None
            runtime.bot.computer = None
            unrelated = runtime.approvals.create(
                "deploy_prod", "high risk deploy from another flow"
            )

            response, traces = _drive(
                runtime.bot,
                "Crea una misión durable de prueba llamada audit-continuation-smoke...",
            )

            assert response
            assert "misión durable" in response
            assert "Procede" in response
            assert unrelated.approval_id not in response
            forbidden = (
                "approval_id",
                "task.contextual_action",
                "needs_approval",
                "pending_approval",
                "waiting_for_user_input",
                "explicit_blocker",
                "/task_approve",
            )
            assert not any(label in response for label in forbidden)
            assert traces
            final_trace = traces[-1]
            assert final_trace["semantic_intent"] == "new_task"
            assert final_trace["approval_scope_match"] == "skipped_new_task"
            assert final_trace["decision"] == "new_task_proposal_created"
            assert final_trace["output_kind"] == "natural_reply"
            assert final_trace["leaked_internal_labels"] == []

            records = runtime.task_ledger.list(session_id="tg-smoke", limit=5)
            assert records
            assert records[0].runtime == "brain_first"
            assert records[0].verification_status == "awaiting_continue"
            assert "audit-continuation-smoke" in records[0].objective
            state = runtime.memory.get_session_state("tg-smoke")
            assert state["pending_action"].startswith("Crea una misión durable")
            assert (
                state["active_object"]["active_mission"]["active_target"]
                == "audit-continuation-smoke"
            )


def test_unscoped_pending_approval_does_not_hijack_continuation() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = build_runtime(anthropic_executor=_fake_anthropic)
            pending = runtime.approvals.create("deploy_prod", "high risk deploy from another flow")

            response, traces = _drive(runtime.bot, "Continúa")

            assert response
            assert pending.approval_id not in response
            assert "approval_id" not in response
            assert "aprobación pendiente" not in response.lower()
            # SOUL routing policy (2026-06-10 audit A1): an unresolved
            # continuation falls through to the brain instead of clarifying.
            assert response == "BRAIN_FALLBACK_USED"
            assert traces[0]["semantic_intent"] == "continue_active_mission"


def test_live_smoke_sequence_resolves_procede_continua_dale_without_generic_loop() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = build_runtime(anthropic_executor=_fake_anthropic)
            runtime.bot.coordinator = None

            first, _ = _drive(
                runtime.bot,
                "Crea una misión durable de prueba llamada audit-continuation-smoke...",
            )
            assert first and "Procede" in first

            for text in ("Procede", "Continúa", "Dale"):
                response, traces = _drive(runtime.bot, text)
                assert response
                lowered = response.lower()
                assert "qué acción concreta" not in lowered
                assert "aprobación pendiente" not in lowered
                assert traces[0]["semantic_intent"] == "continue_active_mission"

            records = runtime.task_ledger.list(session_id="tg-smoke", limit=10)
            assert len(records) >= 1
            assert any(record.runtime == "brain_first" for record in records)
            assert not any(record.runtime == "telegram_preflight" for record in records)


def test_reply_context_dime_y_lo_armo_resolves_armalo_without_generic_loop() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = build_runtime(anthropic_executor=_fake_anthropic)
            runtime.bot.coordinator = None
            reply_text = (
                'Si quieres, convierto el bloque "skills are the prompts + loop engineering" '
                "en el primer post de tu cadena semanal. Dime y lo armo."
            )

            response, traces = _drive(
                runtime.bot,
                "Ármalo",
                context_metadata={
                    "reply_context": {
                        "source": "telegram_reply",
                        "text": reply_text,
                    }
                },
            )

            assert response
            assert "qué acción concreta" not in response.lower()
            assert traces[0]["semantic_intent"] == "continue_active_mission"
            state = runtime.memory.get_session_state("tg-smoke")
            assert "primer post de tu cadena semanal" in state["pending_action"]
