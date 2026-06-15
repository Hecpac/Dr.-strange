from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from claw_v2.adapters.base import LLMRequest
from claw_v2.main import build_runtime
from claw_v2.types import LLMResponse


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


F3B2_PLAN_REQUEST = """OK final: marca F3b.1, F3b.1.5 y F3b.1.5.1 como done.

El daemon ya fue reiniciado y el smoke local paso:
- status query route OK
- imperative route to brain OK
- false positives de "go" cerrados
- F3b.2 no ejecutado
- sin X/LinkedIn/browser/CDP/deploy/GitHub remoto/APIs reales

Opcionalmente ejecuta un live smoke minimo por Telegram:
1. "que tengo pendiente?"
   Esperado: pending handler, sin brain/tool execution.

2. "OK final: routing smoke test solamente. No ejecutes tools. Responde exactamente ROUTING_OK si este mensaje llego al brain."
   Esperado: llega al brain, responde ROUTING_OK, cero tools.

Despues, prepara F3b.2, pero no hagas llamadas reales todavia.

F3b.2 objetivo:
Conectar HeyGenDeliver a provider real read-only/status-only, bajo human gate.

Restricciones:
1. No X/LinkedIn/browser/CDP.
2. No deploy.
3. No GitHub remoto.
4. No publish externo.
5. No borrar recursos.
6. No mutar recursos HeyGen.
7. No gastar creditos.
8. No llamadas reales hasta pedir aprobacion explicita.
9. Si faltan credenciales, provider o permisos, status=blocked.
10. Toda observacion real debe tener evidence_uri y secretos redactados.

Primero entrega plan F3b.2:
- endpoints o fuentes read-only a consultar
- preflight requerido
- external_check real
- evidence_uri
- redaccion de secretos
- tests mock que siguen pasando
- condiciones exactas para succeeded / pending_verification / failed / blocked
"""


def _drive_with_brain_content(content: str) -> tuple[str | None, list[str]]:
    def fake_anthropic(request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content=content,
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
                session_id="tg-plan-status",
                text=F3B2_PLAN_REQUEST,
                runtime_channel="telegram",
            )
            events = [event["event_type"] for event in runtime.observe.recent_events(limit=60)]
            return result, events


def test_ok_final_plan_response_is_not_replaced_by_evidence_gate_template() -> None:
    response, events = _drive_with_brain_content(
        """## F3b.1 + F3b.1.5 + F3b.1.5.1 marcadas done

Evidencia ya cubierta en el turno anterior (smoke JSON + restart receipt + bot.py integrity markers). Cierro la ola F3b.1.x.

## Live smoke por Telegram - pendiente de ti

No puedo enviarme mensajes a mi mismo desde este proceso inline; el live smoke necesita que tu envies los dos textos desde tu app de Telegram.

## Plan F3b.2 - HeyGenDeliver provider real (read-only / status-only)

### Tests mock que siguen pasando
- `tests/test_external_tool_contracts.py`
- `tests/test_f3b1_heygen_deliver_runtime.py`
"""
    )

    assert response is not None
    assert "Plan F3b.2" in response
    assert "marcadas done" in response
    assert "No lo marco como hecho todavia" not in response
    assert "No lo marco como hecho todavía" not in response
    assert "evidence_gate_blocked_completion_claim" not in events
    assert "evidence_gate_explicit_blocker_recorded" not in events
    assert any(
        e in events
        for e in (
            "evidence_gate_skipped_plan_status",
            "evidence_gate_skipped_user_authority",
            "evidence_gate_skipped_content_evidence_ref",
        )
    )


def test_plan_request_still_blocks_explicit_unverified_execution_report() -> None:
    def fake_anthropic(request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content="Listo, hecho. Cambie el archivo y corri tests.",
            lane=request.lane,
            provider="anthropic",
            model=request.model,
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = build_runtime(anthropic_executor=fake_anthropic)
            runtime.bot.coordinator = None
            response = runtime.bot.handle_text(
                user_id="123",
                session_id="tg-plan-status-block",
                text="arregla el bug del ledger",
                runtime_channel="telegram",
            )
            events = [event["event_type"] for event in runtime.observe.recent_events(limit=60)]

    assert response is not None
    assert "Decime qué disparo" in response
    assert "Cambi" not in response
    assert "evidence_gate_blocked_completion_claim" in events
    assert "evidence_gate_skipped_plan_status" not in events
    assert "evidence_gate_skipped_user_authority" not in events
