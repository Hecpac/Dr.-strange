"""B2.1 (bloque F4-B2): shadow-mode delegation-gap telemetry.

Locks the invariant `shadow_delegation_gap_observational` (INTERNAL_WIRING §1):
the detector NEVER blocks, never re-prompts, and never alters the user-visible
text — it only emits `shadow_delegation_gap` events so Hector can decide a
future promotion to enforcement with data.

Fixtures are the live 2026-07-02 baseline messages (daemon DB, mode=ro recon):
msgs 3214/3216/3218/3239 (bare Spanish research asks answered inline) must
classify as research-deliverable asks; msg 3323 (single-URL profile ask,
contract-compliant inline per BROWSER_DELEGATION_RULE) must NOT. The real
baseline turns ran 4 tool calls each (ledger: "brain tool-use turn: 4 tool
calls (unverified)"), which is why the event carries reason=research_inline
alongside reason=no_action.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from claw_v2.adapters.base import LLMRequest
from claw_v2.main import build_runtime
from claw_v2.types import LLMResponse

MSG_3214 = (
    "Investiga a fondo con fuentes web el estado de los agentes de "
    "codificación en 2026 y entrégame un reporte"
)
MSG_3216 = "Investiga a fondo con fuentes web y X el estado sobre sonnet 5 entrégame un reporte"
MSG_3218 = "Investiga sobre el regreso de Fable 5 y hazme un reporte con lo mas relevante"
MSG_3239 = (
    "Investiga las diferencias entre launchctl kickstart y bootstrap y "
    "dame un resumen con recomendación."
)
MSG_3323 = "Investiga este perfil https://x.com/bettatech?s=21"
SALPHA_REPLY = "USA tu propio conocimiento del man page y cierra con 3 lineas"

_PLAIN_BRAIN_ANSWER = (
    "El estado de los agentes de codificación en 2026: hay tres corrientes "
    "principales y ninguna domina todavía. Detalle disponible si lo quieres."
)


def _fake_anthropic(content: str = _PLAIN_BRAIN_ANSWER, artifacts: dict | None = None):
    def _executor(request: LLMRequest) -> LLMResponse:
        response = LLMResponse(
            content=content,
            lane=request.lane,
            provider="anthropic",
            model=request.model,
        )
        if artifacts:
            response.artifacts.update(artifacts)
        return response

    return _executor


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


def _drive(bot, text: str, *, session_id: str = "tg-smoke"):
    """Run handle_text and capture every observe event emitted during the turn."""
    captured: list[tuple[str, dict]] = []
    real_emit = bot.observe.emit

    def spy(event_type: str, **kwargs):
        captured.append((event_type, dict(kwargs.get("payload") or {})))
        return real_emit(event_type, **kwargs)

    with patch.object(bot.observe, "emit", side_effect=spy):
        response = bot.handle_text(
            user_id="123",
            session_id=session_id,
            text=text,
            runtime_channel="telegram",
        )
    return response, captured


def _gap_events(events: list[tuple[str, dict]]) -> list[dict]:
    return [
        payload
        for name, payload in events
        if name == "shadow_delegation_gap" and payload.get("action") == "gap"
    ]


def _runtime(root: Path, executor):
    runtime = build_runtime(anthropic_executor=executor)
    runtime.bot.coordinator = None
    return runtime


# ---------------------------------------------------------------------------
# Detector puro — la clase research-deliverable del baseline
# ---------------------------------------------------------------------------


def test_research_deliverable_detector_matches_baseline_asks() -> None:
    from claw_v2.bot import _looks_like_research_deliverable_ask

    for text in (MSG_3214, MSG_3216, MSG_3218, MSG_3239):
        assert _looks_like_research_deliverable_ask(text), text
    # English arrives too.
    assert _looks_like_research_deliverable_ask(
        "Research the state of coding agents in 2026 and give me a report"
    )


def test_research_deliverable_detector_rejects_non_deliverable_asks() -> None:
    from claw_v2.bot import _looks_like_research_deliverable_ask

    # msg 3323: single-URL profile ask — contract-compliant inline
    # (BROWSER_DELEGATION_RULE); documented design decision: NO gap.
    assert not _looks_like_research_deliverable_ask(MSG_3323)
    # Research verb without a deliverable noun.
    assert not _looks_like_research_deliverable_ask("busca el archivo config")
    # Share-verb false-positive guard: "comparte" is not "compara".
    assert not _looks_like_research_deliverable_ask("comparte el reporte con Tatiana")
    # Plain question.
    assert not _looks_like_research_deliverable_ask("¿Qué hora es?")


# ---------------------------------------------------------------------------
# Integración: handle_text end-to-end (wiring del opt-in en el fallback crudo)
# ---------------------------------------------------------------------------


def test_bare_research_ask_no_tools_emits_gap_no_action() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = _runtime(root, _fake_anthropic())
            response, events = _drive(runtime.bot, MSG_3214)

    gaps = _gap_events(events)
    assert len(gaps) == 1, f"expected exactly one gap event, got {gaps}"
    gap = gaps[0]
    assert gap["reason"] == "no_action"
    assert gap["research_ask"] is True
    assert gap["session_id"] == "tg-smoke"
    assert "source_hash" in gap and "source_preview" in gap
    # OBSERVACIONAL: la respuesta del brain llega intacta al usuario.
    assert response is not None
    assert _PLAIN_BRAIN_ANSWER.strip() in response
    # Y el shadow no toca el evidence gate: cero filas evidence_gate.
    records = runtime.task_ledger.list(session_id="tg-smoke", limit=20)
    runtimes = [getattr(record, "runtime", "") for record in records]
    assert "evidence_gate" not in runtimes


def test_research_ask_with_tools_but_no_delegation_emits_research_inline() -> None:
    artifacts = {"tool_calls": [{"name": "WebFetch"}, {"name": "WebSearch"}]}
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = _runtime(root, _fake_anthropic(artifacts=artifacts))
            response, events = _drive(runtime.bot, MSG_3216)

    gaps = _gap_events(events)
    assert len(gaps) == 1, f"expected exactly one gap event, got {gaps}"
    assert gaps[0]["reason"] == "research_inline"
    assert response is not None
    assert _PLAIN_BRAIN_ANSWER.strip() in response


def test_delegated_turn_emits_no_gap() -> None:
    # Same research ask, but the turn called delegate_task: no gap by definition.
    artifacts = {"tool_calls": [{"name": "mcp__claw__delegate_task"}]}
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = _runtime(root, _fake_anthropic(artifacts=artifacts))
            _, events = _drive(runtime.bot, MSG_3214)

    assert _gap_events(events) == []


def test_action_request_answered_inline_with_tools_no_gap() -> None:
    # Non-research operator action handled inline WITH tool evidence is
    # legitimate inline work (contract: git/fs/logs/local-DB stay inline).
    artifacts = {"tool_calls": [{"name": "Bash"}]}
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = _runtime(
                root,
                _fake_anthropic(
                    content="El ledger tiene 3 entradas stale; aquí el detalle.",
                    artifacts=artifacts,
                ),
            )
            _, events = _drive(runtime.bot, "Limpia el ledger ahora.")

    assert _gap_events(events) == []


def test_knowledge_authorized_reply_no_gap() -> None:
    # The live S-α rescue reply (2026-07-02 01:08, obs 401092 fall_through_all):
    # user redefines the ask as a knowledge answer; answering without tools is
    # compliant, not inaction. Must never count as gap.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = _runtime(
                root,
                _fake_anthropic(content="KeepAlive mantiene el servicio vivo; 3 líneas."),
            )
            _, events = _drive(runtime.bot, SALPHA_REPLY)

    assert _gap_events(events) == []


# ---------------------------------------------------------------------------
# deliver_inline (slice under-delegación 2026-07-03): la clase de bypass que
# la exclusión "action ask handled inline WITH tools — legitimate" hacía
# invisible. Fixtures = los bypasses VIVOS de 2026-07-03 (brain_fallback
# inline, 0 eventos shadow, ledgers 455845/455937 y turno 39e0a5ff20ab6ca0).
# ---------------------------------------------------------------------------

BYPASS_WEB = (
    "Crea DOS archivos HTML self-contained: saludo.html (un saludo corto para "
    "Hector) y fecha.html (la fecha de hoy legible), y envíamelos aquí."
)
BYPASS_TG_1 = (
    "Abre la carpeta de descarga y envíame aquí por telegram el archivo que dice Ar war club"
)
BYPASS_TG_2 = "Ahora envíame el que dice Absolute Recomp"


def test_send_to_owner_detector_matches_live_bypass_asks() -> None:
    from claw_v2.bot import _looks_like_send_to_owner_ask

    for text in (BYPASS_WEB, BYPASS_TG_1, BYPASS_TG_2):
        assert _looks_like_send_to_owner_ask(text), text
    # Variantes de ancla del DELEGATION_CONTRACT + inglés.
    assert _looks_like_send_to_owner_ask("genera el reporte y mándamelo")
    assert _looks_like_send_to_owner_ask("pásame los archivos cuando estén")
    assert _looks_like_send_to_owner_ask("create the report and send it to me")
    # Registro cortés / infinitivo+clítico y reenvío (review SHOULD-FIX #2:
    # el undercount sesga la serie shadow→enforcement hacia abajo).
    assert _looks_like_send_to_owner_ask("puedes enviarme el archivo cuando puedas")
    assert _looks_like_send_to_owner_ask("reenvíame el correo de ayer")
    assert _looks_like_send_to_owner_ask("genera el PDF, quiero mandarme una copia")


def test_send_to_owner_detector_rejects_non_owner_sends() -> None:
    from claw_v2.bot import _looks_like_send_to_owner_ask

    # Acción operativa sin dirección al dueño (la exclusión legítima de hoy).
    assert not _looks_like_send_to_owner_ask("Limpia el ledger ahora.")
    # Envío NO reflexivo (publicar/subir no es entrega al dueño).
    assert not _looks_like_send_to_owner_ask("envía un tweet con el resumen")
    assert not _looks_like_send_to_owner_ask("sube el archivo al server")
    # Pregunta plana.
    assert not _looks_like_send_to_owner_ask("¿Qué hora es?")
    # Falsos positivos morfológicos del regex ampliado.
    assert not _looks_like_send_to_owner_ask("prepárame un resumen")
    assert not _looks_like_send_to_owner_ask("demándame si quieres")
    assert not _looks_like_send_to_owner_ask("recomiéndame un libro")


def test_deliver_ask_inline_with_tools_emits_deliver_inline() -> None:
    # El bypass exacto de 2026-07-03: misión de entrega al dueño trabajada
    # inline CON tools (Write+Bash) y sin delegate_task → gap deliver_inline.
    artifacts = {"tool_calls": [{"name": "Write"}, {"name": "Bash"}]}
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = _runtime(
                root,
                _fake_anthropic(
                    content="Listo, los creé y te los mandé por Telegram.",
                    artifacts=artifacts,
                ),
            )
            response, events = _drive(runtime.bot, BYPASS_WEB)

    gaps = _gap_events(events)
    assert len(gaps) == 1, f"expected exactly one gap event, got {gaps}"
    gap = gaps[0]
    assert gap["reason"] == "deliver_inline"
    assert gap["action_request"] is True
    # OBSERVACIONAL (invariante shadow_delegation_gap_observational): la
    # respuesta del brain llega intacta, el shadow no bloquea nada.
    assert response is not None
    assert "los creé" in response


def test_deliver_ask_delegated_emits_no_gap() -> None:
    artifacts = {"tool_calls": [{"name": "mcp__claw__delegate_task"}]}
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = _runtime(root, _fake_anthropic(artifacts=artifacts))
            _, events = _drive(runtime.bot, BYPASS_WEB)

    assert _gap_events(events) == []


def test_bare_clitic_send_ask_inline_with_tools_emits_deliver_inline() -> None:
    # Review MUST-FIX #1: un follow-up send desnudo ("Ahora mándamelo") no
    # matchea _looks_like_operator_action_request — sin send_ask en el gate,
    # el return temprano hacía deliver_inline inalcanzable para exactamente
    # la clase de BYPASS_TG_2. El gate debe optar-in también por send_ask.
    artifacts = {"tool_calls": [{"name": "Bash"}]}
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = _runtime(
                root,
                _fake_anthropic(content="Te lo mandé por aquí.", artifacts=artifacts),
            )
            _, events = _drive(runtime.bot, "Ahora mándamelo por aquí")

    gaps = _gap_events(events)
    assert len(gaps) == 1, f"expected exactly one gap event, got {gaps}"
    assert gaps[0]["reason"] == "deliver_inline"


def test_research_ask_with_send_anchor_keeps_research_inline_precedence() -> None:
    # NIT #6 del review: research+send → la clase vieja gana (lock del orden).
    artifacts = {"tool_calls": [{"name": "WebFetch"}]}
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = _runtime(root, _fake_anthropic(artifacts=artifacts))
            _, events = _drive(
                runtime.bot,
                "investiga a fondo, hazme un reporte con fuentes y envíamelo",
            )

    gaps = _gap_events(events)
    assert len(gaps) == 1, f"expected exactly one gap event, got {gaps}"
    assert gaps[0]["reason"] == "research_inline"


# ---------------------------------------------------------------------------
# Unidad sobre el helper (exclusiones estructurales)
# ---------------------------------------------------------------------------


def _bare_response(artifacts: dict | None = None) -> LLMResponse:
    response = LLMResponse(
        content=_PLAIN_BRAIN_ANSWER,
        lane="brain",
        provider="anthropic",
        model="claude-opus-4-8",
    )
    if artifacts:
        response.artifacts.update(artifacts)
    return response


def _spy_events(bot):
    captured: list[tuple[str, dict]] = []
    real_emit = bot.observe.emit

    def spy(event_type: str, **kwargs):
        captured.append((event_type, dict(kwargs.get("payload") or {})))
        return real_emit(event_type, **kwargs)

    return captured, patch.object(bot.observe, "emit", side_effect=spy)


def test_not_eligible_turns_never_emit() -> None:
    # Structural exclusion: continuation shortcuts, slash commands and any
    # non-raw-user path do not opt in (eligible=False is the default).
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = _runtime(root, _fake_anthropic())
            captured, spy = _spy_events(runtime.bot)
            with spy:
                runtime.bot._maybe_emit_shadow_delegation_gap(
                    session_id="tg-smoke",
                    source_text=MSG_3214,
                    response=_bare_response(),
                    eligible=False,
                    pre_turn_message_id=0,
                )
    assert [name for name, _ in captured if name == "shadow_delegation_gap"] == []


def test_meta_turn_never_emits() -> None:
    from claw_v2.bot_helpers import meta_introspection_context

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = _runtime(root, _fake_anthropic())
            captured, spy = _spy_events(runtime.bot)
            with spy, meta_introspection_context("meta"):
                runtime.bot._maybe_emit_shadow_delegation_gap(
                    session_id="tg-smoke",
                    source_text=MSG_3214,
                    response=_bare_response(),
                    eligible=True,
                    pre_turn_message_id=0,
                )
    assert [name for name, _ in captured if name == "shadow_delegation_gap"] == []


def test_disabled_knob_emits_disabled_once_and_no_gaps() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        env = dict(_runtime_env(root))
        env["CLAW_SHADOW_DELEGATION_GAP"] = "0"
        with patch.dict(os.environ, env, clear=False):
            runtime = _runtime(root, _fake_anthropic())
            captured, spy = _spy_events(runtime.bot)
            with spy:
                for _ in range(2):
                    runtime.bot._maybe_emit_shadow_delegation_gap(
                        session_id="tg-smoke",
                        source_text=MSG_3214,
                        response=_bare_response(),
                        eligible=True,
                        pre_turn_message_id=0,
                    )
    shadow = [payload for name, payload in captured if name == "shadow_delegation_gap"]
    assert len(shadow) == 1
    assert shadow[0]["action"] == "disabled"


def test_trace_delegate_task_call_is_detected() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = _runtime(root, _fake_anthropic())
            runtime.bot.observe.emit(
                "sdk_post_tool_use",
                trace_id="trace-b21",
                payload={"tool_name": "mcp__claw__delegate_task"},
            )
            response = _bare_response({"trace_id": "trace-b21"})
            assert runtime.bot._turn_trace_called_delegate_task(response) is True
            captured, spy = _spy_events(runtime.bot)
            with spy:
                runtime.bot._maybe_emit_shadow_delegation_gap(
                    session_id="tg-smoke",
                    source_text=MSG_3214,
                    response=response,
                    eligible=True,
                    pre_turn_message_id=0,
                )
    assert _gap_events(captured) == []
