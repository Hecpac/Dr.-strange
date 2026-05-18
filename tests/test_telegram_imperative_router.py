from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claw_v2.adapters.base import LLMRequest
from claw_v2.main import build_runtime
from claw_v2.types import LLMResponse


def _fake_anthropic(request: LLMRequest) -> LLMResponse:
    return LLMResponse(
        content="BRAIN_FALLBACK_USED",
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
            "CLAW_DISABLE_TASK_INTENT_ROUTER": "1",
        }
        with patch.dict(os.environ, env, clear=False):
            runtime = build_runtime(anthropic_executor=_fake_anthropic)
            runtime.bot.coordinator = None
            runtime.bot.computer = None
            runtime.bot.browser_use = None
            yield runtime.bot


def _drive(bot, text: str, *, session_id: str = "tg-test") -> tuple[str | None, list[dict], list[str]]:
    decisions: list[dict] = []
    events: list[str] = []
    real_emit = bot.observe.emit

    def spy(event_type: str, **kwargs):
        events.append(event_type)
        if event_type == "dispatch_decision":
            decisions.append(dict(kwargs.get("payload") or {}))
        return real_emit(event_type, **kwargs)

    with patch.object(bot.observe, "emit", side_effect=spy):
        response = bot.handle_text(
            user_id="123",
            session_id=session_id,
            text=text,
            runtime_channel="telegram",
        )
    return response, decisions, events


def _assert_not_brain_fallback(response: str | None, decisions: list[dict]) -> None:
    assert response != "BRAIN_FALLBACK_USED"
    assert not any(
        ev.get("handler") == "telegram_actionable_task"
        and ev.get("reason") == "telegram_actionable_task_no_match"
        for ev in decisions
    ), decisions


def _seed_codex_mission(bot, session_id: str = "tg-test") -> None:
    bot.brain.memory.update_session_state(
        session_id,
        mode="ops",
        current_goal="Operate Codex app with the latest generated prompt",
        pending_action="Paste latest generated audit prompt into Codex app",
        active_object={
            "active_mission": {
                "mission_id": "mission-codex",
                "channel": "telegram",
                "chat_id": session_id,
                "active_target": "Codex app",
                "active_artifact": "latest generated audit prompt",
                "last_user_goal": "review Codex audit",
                "created_at": time.time(),
                "expires_at": time.time() + 1800,
            },
            "active_prompt": {
                "kind": "prompt",
                "summary": "latest generated audit prompt",
                "text": "Run the phase 3 closeout audit.",
                "created_at": time.time(),
            },
        },
    )


def _set_approval_created_at(bot, approval_id: str, created_at: float) -> None:
    path = bot.approvals.root / f"{approval_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["created_at"] = created_at
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_status_greeting_routes_to_status_not_brain(bot) -> None:
    bot.approvals.create("demo", "approval for status summary")

    response, decisions, events = _drive(bot, "Buen Día, status!")

    assert response
    assert response.strip(" .…") != ""
    assert "Runtime local" in response or "Estoy vivo" in response
    assert "Aprobaciones" in response
    assert any(
        ev.get("handler") == "operational_status" and ev.get("route") == "intercepted"
        for ev in decisions
    ), decisions
    assert "quality_guard_triggered" not in events
    _assert_not_brain_fallback(response, decisions)


def test_failure_summary_routes_to_operational_evidence_not_brain(bot) -> None:
    bot.observe.emit(
        "evidence_gate_blocked_start_claim",
        payload={"session_id": "tg-test", "reason": "start_claim_without_evidence"},
    )
    bot.observe.emit(
        "coordinator_worker_retry",
        payload={
            "task_name": "implement_change",
            "lane": "worker",
            "error": "Codex CLI timed out after 300.0s",
            "attempt": 1,
        },
    )
    bot.brain.memory.store_message(
        "tg-test",
        "assistant",
        "No digo `arrancando` sin haber creado una tarea.",
    )
    bot.task_ledger.create(
        task_id="tg-test:running",
        session_id="tg-test",
        objective="validación de la rama nueva (`brain_shortcut`)",
        mode="coding",
        runtime="coordinator",
        status="running",
    )

    response, decisions, events = _drive(bot, "Haz un resumen de los fallos que haz tenido hoy")

    assert response
    assert "Resumen operativo de fallos de hoy" in response
    assert "Gate de evidencia" in response
    assert "Coordinador" in response
    assert "Codex CLI timed out" in response
    assert "`tg-test:running`" in response
    assert "No digo `arrancando` sin haber creado" not in response
    assert any(
        ev.get("handler") == "operational_failure_summary"
        and ev.get("route") == "intercepted"
        for ev in decisions
    ), decisions
    assert "evidence_gate_blocked_start_claim" not in events
    _assert_not_brain_fallback(response, decisions)


def test_task_completion_complaint_routes_to_operational_evidence_not_brain(bot) -> None:
    bot.task_ledger.create(
        task_id="tg-test:active",
        session_id="tg-test",
        objective="arregla continuidad de tareas",
        mode="coding",
        runtime="coordinator",
        status="running",
    )

    response, decisions, _events = _drive(bot, "Porque no estás completando ninguna tarea")

    assert response
    assert "Resumen operativo de fallos de hoy" in response
    assert "`tg-test:active`" in response
    assert any(
        ev.get("handler") == "operational_failure_summary"
        and ev.get("route") == "intercepted"
        for ev in decisions
    ), decisions
    _assert_not_brain_fallback(response, decisions)


def test_task_status_overview_routes_to_deterministic_summary_not_brain(bot) -> None:
    bot.task_ledger.create(
        task_id="tg-test:failed",
        session_id="tg-test",
        objective="validar brain_shortcut",
        mode="coding",
        runtime="coordinator",
        status="running",
    )
    bot.task_ledger.mark_terminal(
        "tg-test:failed",
        status="failed",
        summary="Codex CLI timed out",
        error="Codex CLI timed out after 300.0s",
        verification_status="failed",
    )

    with patch.object(type(bot.brain), "handle_message", side_effect=AssertionError("brain should not run")):
        response, decisions, _events = _drive(bot, "Estatus de las tareas")

    assert response
    assert "Ahora mismo no tengo tareas corriendo ni en cola" in response
    assert "¿Voy ahora" not in response
    assert any(
        ev.get("handler") == "pending_tasks"
        and ev.get("route") == "intercepted"
        for ev in decisions
    ), decisions
    state = bot.brain.memory.get_session_state("tg-test")
    assert not state.get("pending_action")
    _assert_not_brain_fallback(response, decisions)


def test_task_status_summary_hides_stale_assistant_choice_pending_action(bot) -> None:
    bot.brain.memory.update_session_state(
        "tg-test",
        pending_action=(
            "Voy ahora con eso, o querés que retome alguna otra de las que quedaron perdidas. "
            "Contexto previo: Estatus rápido del ledger."
        ),
        active_object={
            "pending_action_meta": {
                "source": "assistant_proposal_question",
                "created_at": time.time(),
            },
        },
    )

    response, decisions, _events = _drive(bot, "Estatus de las tareas")

    assert response
    assert "Tambien tengo una accion pendiente" not in response
    assert any(
        ev.get("handler") == "pending_tasks"
        and ev.get("route") == "intercepted"
        for ev in decisions
    ), decisions
    _assert_not_brain_fallback(response, decisions)


def test_multimodal_task_completion_complaint_routes_to_operational_evidence_not_brain(bot) -> None:
    bot.task_ledger.create(
        task_id="tg-test:active",
        session_id="tg-test",
        objective="arregla continuidad de tareas",
        mode="coding",
        runtime="coordinator",
        status="running",
    )

    with patch.object(type(bot.brain), "handle_message", side_effect=AssertionError("brain should not run")):
        response = bot.handle_multimodal(
            user_id="123",
            session_id="tg-test",
            content_blocks=[
                {"type": "text", "text": "Porque no estás completando ninguna tarea"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "cG5n",
                    },
                },
            ],
            memory_text="[Imagen adjunta]\nPorque no estás completando ninguna tarea",
            runtime_channel="telegram",
        )

    assert response
    assert "Resumen operativo de fallos de hoy" in response
    assert "`tg-test:active`" in response
    assert response != "BRAIN_FALLBACK_USED"


@pytest.mark.parametrize("text", ["Tareas pendientes", "Tareas pendietes"])
def test_pending_tasks_includes_approval_summary_not_brain(bot, text: str) -> None:
    bot.approvals.create("demo", "approval for pending tasks")

    response, decisions, _events = _drive(bot, text)

    assert response
    assert "Ahora mismo" in response
    assert "aprobacion" in response.lower()
    assert any(
        ev.get("handler") == "pending_tasks" and ev.get("route") == "intercepted"
        for ev in decisions
    ), decisions
    _assert_not_brain_fallback(response, decisions)


def test_contextual_cleanup_archives_stale_and_duplicate_approvals_not_brain(bot) -> None:
    now = time.time()
    latest = bot.approvals.create("verifier_review", "Verifier consensus requires human review")
    duplicate = bot.approvals.create("verifier_review", "Verifier consensus requires human review")
    stale = bot.approvals.create("old_review", "Old low-risk approval")
    _set_approval_created_at(bot, latest.approval_id, now)
    _set_approval_created_at(bot, duplicate.approval_id, now - 3600)
    _set_approval_created_at(bot, stale.approval_id, now - 26 * 3600)

    response, decisions, events = _drive(bot, "Limpia")

    assert response
    assert "approvals.cleanup_stale_duplicates" in response
    assert "Archivadas: 2" in response
    assert bot.approvals.status(latest.approval_id) == "pending"
    assert bot.approvals.status(duplicate.approval_id) == "archived"
    assert bot.approvals.status(stale.approval_id) == "archived"
    assert "approval_cleanup_executed" in events
    assert any(
        ev.get("handler") == "telegram_imperative" and ev.get("route") == "intercepted"
        for ev in decisions
    ), decisions
    _assert_not_brain_fallback(response, decisions)

    status_response, status_decisions, _status_events = _drive(bot, "Limpiaste?")
    assert status_response
    assert "Sí." in status_response
    assert "Archivadas: 2" in status_response
    assert any(
        ev.get("handler") == "cleanup_status" and ev.get("route") == "intercepted"
        for ev in status_decisions
    ), status_decisions
    _assert_not_brain_fallback(status_response, status_decisions)


@pytest.mark.parametrize(
    "text,expected_intent",
    [
        ("Abre la app de Codex", "ui.open_app"),
        ("Abre Codex", "ui.open_app"),
        ("Revisa la app", "ui.inspect_app"),
        ("Revisa Codex", "ui.inspect_app"),
    ],
)
def test_clear_app_imperatives_route_to_result_not_brain(bot, text: str, expected_intent: str) -> None:
    _seed_codex_mission(bot)

    response, decisions, events = _drive(bot, text)

    assert response
    assert expected_intent in response or "blocked_by_capability" in response or "Tarea" in response
    assert "telegram_imperative_detected" in events
    assert "telegram_imperative_routed" in events or "telegram_imperative_blocked" in events
    assert any(
        ev.get("handler") == "telegram_imperative" and ev.get("route") == "intercepted"
        for ev in decisions
    ), decisions
    _assert_not_brain_fallback(response, decisions)


@pytest.mark.parametrize("text", ["Dale las instructions", "Dale las instrucciones"])
def test_give_instructions_resolves_active_mission(bot, text: str) -> None:
    _seed_codex_mission(bot)

    response, decisions, events = _drive(bot, text)

    assert response
    assert "Codex" in response
    assert "prompt" in response.lower() or "instructions" in response.lower() or "instrucciones" in response.lower()
    assert "active_mission_resolution_success" in events
    _assert_not_brain_fallback(response, decisions)


@pytest.mark.parametrize("text", ["Pégale el prompt", "Pega el prompt", "Paste the prompt"])
def test_paste_prompt_is_paste_only_and_does_not_claim_clipboard_as_full_success(bot, text: str) -> None:
    _seed_codex_mission(bot)

    response, decisions, events = _drive(bot, text)

    assert response
    assert "ui.paste_text" in response or "ui.paste_clipboard" in response
    assert "ui.submit_prompt" not in response
    assert "mandado" not in response.lower()
    assert "enviado" not in response.lower()
    assert "blocked_by_capability" in response or "partial_success" in response or "Tarea" in response
    assert "telegram_imperative_detected" in events
    _assert_not_brain_fallback(response, decisions)


@pytest.mark.parametrize("text", ["Mándalo", "Dale enter"])
def test_submit_prompt_is_distinct_from_paste(bot, text: str) -> None:
    _seed_codex_mission(bot)

    response, decisions, _events = _drive(bot, text)

    assert response
    assert "ui.submit_prompt" in response
    assert "ui.paste_text" not in response
    assert "approval" in response.lower() or "blocked_by_capability" in response
    _assert_not_brain_fallback(response, decisions)


@pytest.mark.parametrize("text", ["Córrelo tú", "Correlo tu", "Hazlo tú", "Encárgate tú"])
def test_owner_delegation_never_falls_back(bot, text: str) -> None:
    _seed_codex_mission(bot)

    response, decisions, events = _drive(bot, text)

    assert response
    assert "owner_delegation_match" in events
    assert any(
        ev.get("handler") == "owner_delegation" and ev.get("route") == "intercepted"
        for ev in decisions
    ), decisions
    _assert_not_brain_fallback(response, decisions)


def test_english_owner_delegation_with_options_never_falls_back(bot) -> None:
    bot.brain.memory.update_session_state(
        "tg-test",
        last_options=["summarize local notes", "export metrics to local csv"],
        active_object={"last_options_meta": {"created_at": time.time()}},
    )

    response, decisions, events = _drive(bot, "You decide")

    assert response
    assert "owner_delegation_match" in events
    _assert_not_brain_fallback(response, decisions)


def test_explicit_imperative_bypasses_disabled_task_intent_flag(bot) -> None:
    _seed_codex_mission(bot)

    response, decisions, _events = _drive(bot, "Pégale el prompt")

    task_intent_events = [ev for ev in decisions if ev.get("handler") == "task_intent"]
    assert task_intent_events == []
    _assert_not_brain_fallback(response, decisions)


def test_actionable_no_match_falls_through_to_brain(bot) -> None:
    """B: imperative router no longer emits a robotic diagnostic template;
    actionable-but-unmapped messages fall through so the brain can answer
    naturally. Telemetry events for the no-match decision are preserved."""
    response, decisions, events = _drive(bot, "Orquesta eso en la otra app rara")

    assert response
    assert "no pude mapearla" not in response.lower()
    assert "acción probable" not in response.lower()
    assert "target probable" not in response.lower()
    assert "actionable_no_match" in events
    assert any(
        ev.get("handler") == "telegram_imperative"
        and ev.get("reason") == "actionable_no_match"
        and ev.get("route") == "fall_through"
        for ev in decisions
    ), decisions


def test_continue_sends_pending_action_context_to_brain_without_autonomous_task(bot) -> None:
    bot.brain.memory.update_session_state(
        "tg-test",
        mode="ops",
        current_goal="arreglar continuation imperative router bounce",
        pending_action="arreglar #6 continuation imperative router bounce en bot.py",
    )

    response, decisions, events = _drive(bot, "Continúa")

    assert response
    assert response == "BRAIN_FALLBACK_USED"
    assert "Necesito una aclaración mínima" not in response
    assert "telegram_continuation_stateful_resolved" in events
    assert "stateful_continuation_routed_to_actionable_task" not in events
    records = bot.task_ledger.list(session_id="tg-test", limit=5)
    assert records == []
    assert any(
        ev.get("handler") == "telegram_imperative"
        and ev.get("route") == "intercepted"
        and ev.get("reason") == "telegram_imperative:task.continue_active_mission:stateful"
        for ev in decisions
    ), decisions


def test_continue_uses_recent_contextual_proposal_in_telegram(bot) -> None:
    bot.brain.memory.store_message(
        "tg-test",
        "assistant",
        (
            "**Estado del check-list:**\n"
            "- ✅ #3 dispatch_typed migration → `606d648`\n"
            "- 🟡 #4 política \"default=brain\" en SOUL/AGENTS → siguiente\n"
            "- 🟡 #6 \"Procede\" / continuation imperative router bounce → bot.py audit\n\n"
            "¿Sigo con #4 (política en SOUL/AGENTS) o querés que en su lugar arregle #6?"
        ),
    )

    response, decisions, events = _drive(bot, "Continúa")

    assert response
    assert response == "BRAIN_FALLBACK_USED"
    assert "telegram_continuation_stateful_resolved" in events
    assert "stateful_continuation_routed_to_actionable_task" not in events
    records = bot.task_ledger.list(session_id="tg-test", limit=5)
    assert records == []
    assert any(
        ev.get("handler") == "telegram_imperative"
        and ev.get("route") == "intercepted"
        and ev.get("reason") == "telegram_imperative:task.continue_active_mission:stateful"
        for ev in decisions
    ), decisions


def test_continue_uses_reply_context_markdown_pending_line(bot) -> None:
    reply_context = (
        "**Checkpoint:**\n"
        "- **Hecho:** inspeccion de observe_stream post-restart.\n"
        "- **Pendiente:** validacion de la rama nueva (`brain_shortcut`) "
        "— requiere un \"Procede\"/\"Continua\" pelado de tu parte. Sigo activo esperando."
    )

    bot.brain.memory.update_session_state(
        "tg-test",
        active_object={
            "reply_context": {
                "source": "telegram_reply",
                "text": reply_context,
                "created_at": time.time(),
            }
        },
    )

    response, decisions, events = _drive(bot, "Continúa")

    assert response
    assert response == "BRAIN_FALLBACK_USED"
    assert "telegram_continuation_stateful_resolved" in events
    assert "stateful_continuation_routed_to_actionable_task" not in events
    assert "¿Qué acción concreta" not in response
    records = bot.task_ledger.list(session_id="tg-test", limit=5)
    assert records == []
    assert any(
        ev.get("handler") == "telegram_imperative"
        and ev.get("route") == "intercepted"
        and ev.get("reason") == "telegram_imperative:task.continue_active_mission:stateful"
        for ev in decisions
    ), decisions


def _assert_valid_continuation_output(response: str | None) -> None:
    assert response
    lowered = response.lower()
    assert "¿qué acción concreta quieres que ejecute?" not in lowered
    assert "target: `desconocido`" not in lowered
    assert "target desconocido" not in lowered


def test_replay_voy_con_numero_procede_creates_durable_task(bot) -> None:
    bot.brain.memory.store_message(
        "tg-test",
        "assistant",
        "Voy con #3: auditar el router de continuaciones y preparar el parche. ¿Lo arranco?",
    )

    response, decisions, events = _drive(bot, "Procede")

    _assert_valid_continuation_output(response)
    assert response == "BRAIN_FALLBACK_USED"
    assert "telegram_continuation_stateful_resolved" in events
    records = bot.task_ledger.list(session_id="tg-test", limit=5)
    assert records == []
    assert any(
        ev.get("handler") == "telegram_imperative"
        and ev.get("reason") == "telegram_imperative:task.continue_active_mission:stateful"
        for ev in decisions
    ), decisions


def test_replay_contextual_choice_continua_chooses_single_proposal(bot) -> None:
    bot.brain.memory.store_message(
        "tg-test",
        "assistant",
        "¿Sigo con #4 o arreglo #6?",
    )

    response, _decisions, events = _drive(bot, "Continúa")

    _assert_valid_continuation_output(response)
    assert response == "BRAIN_FALLBACK_USED"
    assert "telegram_continuation_stateful_resolved" in events
    records = bot.task_ledger.list(session_id="tg-test", limit=5)
    assert records == []


def test_replay_pegalo_y_enviamelo_uses_active_prompt_or_blocks_explicitly(bot) -> None:
    bot.brain.memory.update_session_state(
        "tg-test",
        mode="ops",
        active_object={
            "active_mission": {
                "mission_id": "mission-claude",
                "channel": "telegram",
                "chat_id": "tg-test",
                "active_target": "Claude",
                "pending_action": "pegar prompt preparado en Claude",
                "created_at": time.time(),
                "expires_at": time.time() + 1800,
            },
            "active_prompt": {
                "kind": "prompt",
                "summary": "prompt preparado",
                "text": "Construye el prototipo y devuelve el resultado.",
            },
            "reply_context": {
                "source": "telegram_reply",
                "text": "Tengo el prompt listo para Claude. ¿Lo pego ahora?",
                "created_at": time.time(),
            },
        },
    )
    bot.computer = MagicMock()
    bot.browser_use = None
    bot.computer_gate = MagicMock()

    with (
        patch(
            "claw_v2.bot.subprocess.run",
            return_value=subprocess.CompletedProcess(["ok"], 0, "", ""),
        ) as run,
        patch("claw_v2.bot.time.sleep"),
    ):
        response, decisions, events = _drive(bot, "Pégalo y envíamelo aquí")

    _assert_valid_continuation_output(response)
    assert "ui.paste_text" in response
    assert "succeeded" in response
    assert "Texto pegado en `Claude` sin enviar." in response
    assert run.call_args_list[1].args[0] == ["pbcopy"]
    assert run.call_args_list[1].kwargs["input"] == "Construye el prototipo y devuelve el resultado."
    assert "telegram_imperative_executed" in events
    _assert_not_brain_fallback(response, decisions)


def test_replay_revisa_en_google_cloud_has_explicit_target_blocker(bot) -> None:
    response, decisions, events = _drive(bot, "Revisa en Google Cloud")

    _assert_valid_continuation_output(response)
    assert "ui.inspect_app" in response
    assert "Google Cloud" in response
    assert "blocked_by_capability" in response
    assert "telegram_imperative_blocked" in events
    _assert_not_brain_fallback(response, decisions)


def test_replay_waiting_for_user_input_task_continua_resumes_task(bot) -> None:
    bot.task_ledger.create(
        task_id="tg-test:waiting",
        session_id="tg-test",
        objective="terminar auditoría P0 de Telegram continuation",
        mode="coding",
        runtime="coordinator",
        status="running",
    )
    bot.task_ledger.mark_terminal(
        "tg-test:waiting",
        status="failed",
        summary="waiting_for_user_input: confirmar siguiente paso",
        error="waiting_for_user_input: confirmar siguiente paso",
        verification_status="blocked",
    )

    response, _decisions, events = _drive(bot, "Continúa")

    _assert_valid_continuation_output(response)
    assert response == "BRAIN_FALLBACK_USED"
    assert "telegram_continuation_stateful_resolved" in events
    records = bot.task_ledger.list(session_id="tg-test", limit=5)
    assert records
    assert records[0].objective == "terminar auditoría P0 de Telegram continuation"
    assert not any(record.runtime == "telegram_preflight" for record in records)


def test_multiple_active_missions_asks_specific_choice_not_generic_action(bot) -> None:
    bot.brain.memory.update_session_state(
        "tg-test",
        active_object={
            "active_missions": [
                {
                    "mission_id": "m1",
                    "channel": "telegram",
                    "chat_id": "tg-test",
                    "active_target": "Codex",
                    "pending_action": "arreglar el router",
                    "expires_at": time.time() + 1800,
                },
                {
                    "mission_id": "m2",
                    "channel": "telegram",
                    "chat_id": "tg-test",
                    "active_target": "Claude",
                    "pending_action": "pegar el prompt",
                    "expires_at": time.time() + 1800,
                },
            ]
        },
    )

    response, _decisions, events = _drive(bot, "Procede")

    _assert_valid_continuation_output(response)
    assert "varias misiones activas" in response
    assert "1. Codex: arreglar el router" in response
    assert "2. Claude: pegar el prompt" in response
    assert "telegram_continuation_stateful_resolved" in events


def test_quality_command_exposes_imperative_router_metrics(bot) -> None:
    _seed_codex_mission(bot)
    _drive(bot, "Pégale el prompt")

    payload = json.loads(bot.handle_text(user_id="123", session_id="tg-test", text="/quality"))

    routing = payload["autonomy_routing"]
    assert "telegram_imperative_detected_total" in routing
    assert "telegram_actionable_no_match_total" in routing
    assert "telegram_imperative_executed_total" in routing
    assert "telegram_imperative_pending_approval_total" in routing
    assert "telegram_imperative_execution_failed_total" in routing
    assert "brain_fallback_for_actionable_total" in routing
    assert routing["brain_fallback_for_actionable_total"] == 0


def test_open_app_imperative_uses_local_open_without_approval(bot) -> None:
    bot.computer = MagicMock()
    bot.browser_use = None
    bot.computer_gate = MagicMock()

    with patch(
        "claw_v2.bot.subprocess.run",
        return_value=subprocess.CompletedProcess(["open", "-a", "Claude"], 0, "", ""),
    ) as run:
        response, decisions, events = _drive(bot, "Abre Claude")

    assert response
    assert "ui.open_app" in response
    assert "succeeded" in response
    assert "pending_approval" not in response
    assert "Necesito tu autorización" not in response
    assert "`Claude` abierto/enfocado." in response
    run.assert_called_once_with(["open", "-a", "Claude"], capture_output=True, text=True, timeout=10)
    bot.computer.run_agent_loop.assert_not_called()
    assert bot.approvals.list_pending() == []
    assert "telegram_imperative_executed" in events
    _assert_not_brain_fallback(response, decisions)


def test_open_claude_design_does_not_route_to_desktop_app(bot) -> None:
    bot.computer = MagicMock()
    bot.browser_use = None
    bot.computer_gate = MagicMock()

    with patch("claw_v2.bot.subprocess.run") as run:
        response, _decisions, events = _drive(bot, "Abre Claude/design")

    run.assert_not_called()
    assert "ui.open_app" not in (response or "")
    assert "telegram_imperative_executed" not in events


def test_open_chrome_claude_design_does_not_route_to_desktop_app(bot) -> None:
    bot.computer = MagicMock()
    bot.browser_use = None
    bot.computer_gate = MagicMock()

    with patch("claw_v2.bot.subprocess.run") as run:
        response, _decisions, events = _drive(bot, "Abre en chrome Claude/design")

    run.assert_not_called()
    assert "ui.open_app" not in (response or "")
    assert "telegram_imperative_executed" not in events


def test_paste_prompt_imperative_executes_local_paste_without_approval(bot) -> None:
    _seed_codex_mission(bot)
    bot.computer = MagicMock()
    bot.browser_use = None
    bot.computer_gate = MagicMock()

    with (
        patch(
            "claw_v2.bot.subprocess.run",
            return_value=subprocess.CompletedProcess(["ok"], 0, "", ""),
        ) as run,
        patch("claw_v2.bot.time.sleep"),
    ):
        response, decisions, events = _drive(bot, "Pégale el prompt")

    assert response
    assert "ui.paste_text" in response
    assert "ui.submit_prompt" not in response
    assert "succeeded" in response
    assert "pending_approval" not in response
    assert "Texto pegado en `Codex` sin enviar." in response
    assert run.call_args_list[0].args[0] == ["open", "-a", "Codex"]
    assert run.call_args_list[1].args[0] == ["pbcopy"]
    assert run.call_args_list[1].kwargs["input"] == "Run the phase 3 closeout audit."
    assert run.call_args_list[2].args[0][0] == "osascript"
    bot.computer.run_agent_loop.assert_not_called()
    assert bot.approvals.list_pending() == []
    assert "telegram_imperative_executed" in events
    _assert_not_brain_fallback(response, decisions)


def test_pegalo_uses_reply_context_prompt_not_brain(bot) -> None:
    reply_context = (
        "Voy con A. Preparando el prompt para Claude/design y lo pego en la ventana abierta sin submit.\n\n"
        "**Prompt que voy a pegar:**\n\n"
        "> Build a single-page interactive prototype for an AI Lead Generation product.\n"
        "> Use Next.js, Tailwind, mock lead cards, and a postal preview.\n"
    )
    bot.brain.memory.update_session_state(
        "tg-test",
        mode="ops",
        active_object={
            "active_mission": {
                "mission_id": "mission-claude",
                "channel": "telegram",
                "chat_id": "tg-test",
                "active_target": "Claude",
                "last_user_goal": "create AI lead gen prototype in Claude/design",
                "created_at": time.time(),
                "expires_at": time.time() + 1800,
            },
            "reply_context": {
                "source": "telegram_reply",
                "text": reply_context,
                "created_at": time.time(),
            },
        },
    )
    bot.computer = MagicMock()
    bot.browser_use = None
    bot.computer_gate = MagicMock()

    with (
        patch(
            "claw_v2.bot.subprocess.run",
            return_value=subprocess.CompletedProcess(["ok"], 0, "", ""),
        ) as run,
        patch("claw_v2.bot.time.sleep"),
    ):
        response, decisions, events = _drive(bot, "Pégalo y veamos Que nos da y me lo envias Aqui en telegram")

    assert response
    assert "ui.paste_text" in response
    assert "succeeded" in response
    assert "BRAIN_FALLBACK_USED" not in response
    assert "pending_approval" not in response
    assert "Texto pegado en `Claude` sin enviar." in response
    assert run.call_args_list[0].args[0] == ["open", "-a", "Claude"]
    assert run.call_args_list[1].args[0] == ["pbcopy"]
    assert "Build a single-page interactive prototype" in run.call_args_list[1].kwargs["input"]
    assert run.call_args_list[2].args[0][0] == "osascript"
    bot.computer.run_agent_loop.assert_not_called()
    assert "telegram_imperative_executed" in events
    assert any(
        ev.get("handler") == "telegram_imperative" and ev.get("route") == "intercepted"
        for ev in decisions
    ), decisions
    _assert_not_brain_fallback(response, decisions)


def test_inspect_app_imperative_uses_computer_read_when_available(bot) -> None:
    _seed_codex_mission(bot)
    bot.computer = MagicMock()
    bot.computer.capture_screenshot.return_value = {
        "data": "abc123",
        "media_type": "image/png",
    }

    with patch.object(
        type(bot.brain),
        "handle_message",
        return_value=LLMResponse(
            content="Codex app is visible and idle.",
            lane="brain",
            provider="anthropic",
            model="claude-opus-4-7",
        ),
    ) as mock_handle_message:
        response, decisions, events = _drive(bot, "Revisa la app")

    assert response
    assert "ui.inspect_app" in response
    assert "Codex app is visible and idle." in response
    bot.computer.capture_screenshot.assert_called_once_with()
    mock_handle_message.assert_called_once()
    assert "telegram_imperative_executed" in events
    _assert_not_brain_fallback(response, decisions)


def test_submit_imperative_uses_computer_approval_path_when_available(bot) -> None:
    _seed_codex_mission(bot)
    bot.computer = MagicMock()
    bot.computer.capture_screenshot.return_value = {
        "data": "iVBORw0KGgo=",
        "media_type": "image/png",
    }
    bot.browser_use = None
    bot.computer_gate = MagicMock()

    def fake_run_agent_loop(*, session, **_kwargs):
        session.status = "awaiting_approval"
        session.pending_action = {
            "tool_use_id": "tool-1",
            "action": "keypress",
            "keys": "ENTER",
        }
        return "Action needs approval: keypress — waiting"

    bot.computer.run_agent_loop.side_effect = fake_run_agent_loop

    response, decisions, events = _drive(bot, "Dale enter")

    assert response
    assert "ui.submit_prompt" in response
    assert "pending_approval" in response
    assert "Necesito tu autorización" in response
    assert bot.approvals.list_pending()
    assert "telegram_imperative_pending_approval" in events
    _assert_not_brain_fallback(response, decisions)
