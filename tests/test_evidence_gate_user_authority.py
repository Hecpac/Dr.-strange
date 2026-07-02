from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from claw_v2.adapters.base import LLMRequest
from claw_v2.bot import (
    BotService,
    _brain_content_references_evidence,
    _user_authoritatively_marked_done,
    _user_authorized_knowledge_answer,
)
from claw_v2.main import build_runtime
from claw_v2.types import LLMResponse


class TestUserAuthoritativelyMarkedDone:
    def test_ok_final_marca_done(self) -> None:
        assert _user_authoritatively_marked_done(
            "OK final: marca F3b.1, F3b.1.5 y F3b.1.5.1 como done. Reiniciado y smoke local pasó."
        )

    def test_marca_x_como_succeeded(self) -> None:
        assert _user_authoritatively_marked_done("OK final: marca F3b.0 como SUCCEEDED.")

    def test_marca_como_listo(self) -> None:
        assert _user_authoritatively_marked_done("marca la pista como listo, cerramos.")

    def test_deja_x_como_done(self) -> None:
        assert _user_authoritatively_marked_done("Deja la fase F2 como done y abrimos la 3.")

    def test_ya_quedo_done(self) -> None:
        assert _user_authoritatively_marked_done("ya quedó listo, sigamos.")

    def test_mark_as_done_english(self) -> None:
        assert _user_authoritatively_marked_done("mark phase F4 as done")

    def test_regular_action_request_does_not_match(self) -> None:
        assert not _user_authoritatively_marked_done("hazlo")
        assert not _user_authoritatively_marked_done("publica el thread")
        assert not _user_authoritatively_marked_done("Dale credential check")

    def test_empty_input(self) -> None:
        assert not _user_authoritatively_marked_done("")
        assert not _user_authoritatively_marked_done(None)  # type: ignore[arg-type]


class TestUserAuthorizedKnowledgeAnswer:
    def test_usa_tu_propio_conocimiento_live_case(self) -> None:
        # Caso real 2026-07-02 01:08 UTC (obs 401107): esta autorización fue
        # suprimida por el gate y rompió el rescate S-α.
        assert _user_authorized_knowledge_answer(
            "USA tu propio conocimiento del man page y cierra con 3 lineas"
        )

    def test_usa_tu_conocimiento(self) -> None:
        assert _user_authorized_knowledge_answer("usa tu conocimiento y dame el resumen")

    def test_de_tu_conocimiento(self) -> None:
        assert _user_authorized_knowledge_answer("dámelo de tu propio conocimiento")

    def test_de_memoria(self) -> None:
        assert _user_authorized_knowledge_answer("respóndeme de memoria, sin buscar nada")

    def test_responde_directo(self) -> None:
        assert _user_authorized_knowledge_answer("respóndelo directo tú")

    def test_sin_tools(self) -> None:
        assert _user_authorized_knowledge_answer("dame la respuesta sin tools")

    def test_sin_usar_herramientas(self) -> None:
        assert _user_authorized_knowledge_answer("explícalo sin usar herramientas")

    def test_english_use_your_knowledge(self) -> None:
        assert _user_authorized_knowledge_answer("use your own knowledge and close it")

    def test_english_from_memory(self) -> None:
        assert _user_authorized_knowledge_answer("answer it from memory")

    def test_action_requests_do_not_match(self) -> None:
        assert not _user_authorized_knowledge_answer("hazlo")
        assert not _user_authorized_knowledge_answer("publica el thread")
        assert not _user_authorized_knowledge_answer(
            "“Delega esto como tarea autónoma (delegate_task, modo research): "
            "investiga qué es launchd KeepAlive y resume en 3 líneas.”"
        )

    def test_negated_knowledge_request_does_not_match(self) -> None:
        assert not _user_authorized_knowledge_answer("no uses tu conocimiento, ejecútalo real")

    def test_empty_input(self) -> None:
        assert not _user_authorized_knowledge_answer("")
        assert not _user_authorized_knowledge_answer(None)  # type: ignore[arg-type]


class TestBrainContentReferencesEvidence:
    def test_artifacts_verification_path(self) -> None:
        assert _brain_content_references_evidence(
            "evidence en artifacts/verification/f3b2/20260526T210323_correlation.json"
        )

    def test_artifacts_heygen_path(self) -> None:
        assert _brain_content_references_evidence("descargado a artifacts/heygen/video_1779.mp4")

    def test_artifacts_x_sweep_path(self) -> None:
        assert _brain_content_references_evidence(
            "barrido completo en artifacts/x_sweep/x_sweep_1779832313.json"
        )

    def test_evidence_uri_inline(self) -> None:
        assert _brain_content_references_evidence(
            "status: blocked\nevidence_uri: artifacts/verification/f3b2/x.json"
        )

    def test_f3b_receipt_filename(self) -> None:
        assert _brain_content_references_evidence(
            "el receipt f3b1_reconcile_1779817374.log confirma 161/161 passed"
        )

    def test_checkpoint_marker(self) -> None:
        assert _brain_content_references_evidence("listo.\n**Checkpoint:**\n- todo verde")

    def test_correlation_id(self) -> None:
        assert _brain_content_references_evidence(
            "correlation_id: 1b6484baa11c41d2b78b874df3514f6f registrado"
        )

    def test_msg_id_telegram(self) -> None:
        assert _brain_content_references_evidence("enviado a Telegram msg_id 10891")

    def test_db_reference(self) -> None:
        assert _brain_content_references_evidence(
            "grant persisted in data/claw.db tabla capability_grants"
        )

    def test_plain_completion_claim_does_not_match(self) -> None:
        assert not _brain_content_references_evidence("Listo, lo hice")
        assert not _brain_content_references_evidence("Voy a arrancar el thread")
        assert not _brain_content_references_evidence("Publicado.")

    def test_empty_input(self) -> None:
        assert not _brain_content_references_evidence("")
        assert not _brain_content_references_evidence(None)  # type: ignore[arg-type]


class TestEvidenceGateTemplatesAreShort:
    def test_pending_evidence_response_is_informative_and_terse(self) -> None:
        bot = BotService.__new__(BotService)
        msg = bot._pending_evidence_response(task_id=None)
        assert len(msg) < 200
        assert "Decime" not in msg
        assert "No lo marco como hecho todavía" not in msg
        assert "Retuve mi respuesta" in msg
        assert "usa tu conocimiento" in msg
        assert "evidencia" in msg.lower()

    def test_unexecuted_start_response_is_informative_and_terse(self) -> None:
        bot = BotService.__new__(BotService)
        msg = bot._unexecuted_start_response(task_id=None)
        assert len(msg) < 200
        assert "Decime" not in msg
        assert "No arranqué nada todavía" not in msg
        assert "evidencia" in msg.lower()


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


def _drive(source_text: str, brain_content: str) -> tuple[str | None, list[str]]:
    def fake_anthropic(request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content=brain_content,
            lane=request.lane,
            provider="anthropic",
            model=request.model,
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = build_runtime(anthropic_executor=fake_anthropic)
            runtime.bot.coordinator = None
            result = runtime.bot.handle_text(
                user_id="123",
                session_id="tg-knowledge-authority",
                text=source_text,
                runtime_channel="telegram",
            )
            events = [event["event_type"] for event in runtime.observe.recent_events(limit=60)]
            return result, events


KNOWLEDGE_CLOSE_CONTENT = (
    "Hecho, de mi propio conocimiento del `launchd.plist(5)`:\n"
    "1. **ThrottleInterval** — intervalo mínimo en segundos (default 10) que launchd "
    "respeta entre relanzamientos del mismo job; cerré la tarea con esto.\n"
    "2. **ProcessType** — clasifica el job (Background/Standard/Adaptive/Interactive) "
    "y define su QoS.\n"
    "3. Recomendación: ProcessType Standard y ThrottleInterval 10 para com.pachano.claw; "
    "no ejecuté el comando `man` en este turno."
)


def test_user_authorized_knowledge_close_is_delivered() -> None:
    """Invariante evidence_gate_user_knowledge_authority (lado permisivo):

    cuando el usuario autoriza explícitamente una respuesta de conocimiento
    propio en el turno actual, el gate registra el skip auditado y el
    entregable llega — no el stub. Regresión del 2026-07-02 (obs 401107).
    """
    response, events = _drive(
        "USA tu propio conocimiento del man page y cierra con 3 lineas",
        KNOWLEDGE_CLOSE_CONTENT,
    )
    assert response is not None
    assert "ThrottleInterval" in response
    assert "Retuve mi respuesta" not in response
    assert "Decime qué disparo" not in response
    assert "evidence_gate_blocked_completion_claim" not in events
    assert "evidence_gate_skipped_user_authority" in events


def test_unauthorized_completion_claim_stays_blocked() -> None:
    """Invariante evidence_gate_user_knowledge_authority (lado restrictivo):

    sin autorización del usuario en el turno, un claim de completación sin
    evidencia sigue suprimido (F4-B1 no se debilita) y el stub informa qué
    pasó y cómo salir.
    """
    response, events = _drive(
        "arregla el bug del ledger",
        "Listo, hecho. Cambie el archivo y corri tests.",
    )
    assert response is not None
    assert "Cambi" not in response
    assert "Retuve mi respuesta" in response
    assert "evidence_gate_blocked_completion_claim" in events
    assert "evidence_gate_skipped_user_authority" not in events
