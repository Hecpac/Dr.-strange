"""P0-1 integration regression: meta_introspection_guard must not cause
evidence-gate to (a) pollute agent_tasks with `runtime=evidence_gate` rows,
(b) replace the brain response with a robotic template that exposes internal
task IDs, while still (c) emitting an observability event for the
self-improvement loop.

Counterfactual test in the same module asserts the gate stays armed for
non-meta operator action requests.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from claw_v2.adapters.base import LLMRequest
from claw_v2.main import build_runtime
from claw_v2.types import LLMResponse


# Phrase that trips _looks_like_starting_side_effect_claim:
#   - "voy a" matches _STARTING_ACTION_CLAIM_PATTERNS
#   - "limpiar" / "ledger" matches _STARTING_ACTION_OBJECT_PATTERNS
# The brain stub returns this regardless of the prompt; only the source_text
# decides whether meta_introspection_guard fires.
_BRAIN_START_CLAIM = "Voy a limpiar el ledger y aplico los fixes ahora."


def _fake_anthropic_returning_start_claim(request: LLMRequest) -> LLMResponse:
    return LLMResponse(
        content=_BRAIN_START_CLAIM,
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


def _drive(bot, text: str, *, session_id: str = "tg-smoke") -> tuple[str | None, list[tuple[str, dict]]]:
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


def _event_types(events: list[tuple[str, dict]]) -> list[str]:
    return [name for name, _ in events]


def test_complaint_no_evidence_gate_task() -> None:
    """Hector's complaint shape must not generate an evidence-gate failed task,
    must not have its brain response replaced by the robotic blocker template,
    and must still emit `evidence_gate_skipped_meta` so self-improvement keeps
    the signal.
    """
    complaint = "¿por qué no completas las tareas que te pido?"

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = build_runtime(
                anthropic_executor=_fake_anthropic_returning_start_claim
            )
            runtime.bot.coordinator = None

            response, events = _drive(runtime.bot, complaint)

    # (a) No evidence-gate row in the ledger for this session.
    records = runtime.task_ledger.list(session_id="tg-smoke", limit=20)
    runtimes = [getattr(record, "runtime", "") for record in records]
    assert "evidence_gate" not in runtimes, (
        f"meta turn should not create evidence_gate ledger row; got runtimes={runtimes}"
    )

    # (b) Brain response delivered intact — no robotic template, no internal IDs.
    assert response is not None
    forbidden_fragments = (
        "explicit_blocker",
        "Bloqueé el arranque",
        "Bloqueé esa respuesta",
        "evidence-gate:",
        "Task: `tg-",
    )
    for fragment in forbidden_fragments:
        assert fragment not in response, (
            f"meta-turn response leaked forbidden fragment {fragment!r}: {response!r}"
        )
    # Brain content survived (modulo trailing whitespace / sanitizer no-ops).
    assert _BRAIN_START_CLAIM.strip() in response, (
        f"meta-turn must surface brain content; got: {response!r}"
    )

    # (c) Observability still fired: routed_to_chat + skipped_meta with the
    # right reason+kind; the blocker event must be absent.
    types = _event_types(events)
    assert "meta_introspection_routed_to_chat" in types
    assert "evidence_gate_skipped_meta" in types
    assert "evidence_gate_blocked_start_claim" not in types
    assert "evidence_gate_explicit_blocker_recorded" not in types

    skipped = [payload for name, payload in events if name == "evidence_gate_skipped_meta"]
    assert skipped, "expected at least one evidence_gate_skipped_meta event"
    skip_payload = skipped[0]
    assert skip_payload.get("meta_kind") == "meta"
    assert skip_payload.get("reason") == "start_claim_without_evidence"
    assert skip_payload.get("session_id") == "tg-smoke"


def test_non_meta_operator_request_still_trips_evidence_gate() -> None:
    """Counterfactual: an operative imperative without meta_introspection
    context must keep the evidence-gate fully active (row + replacement +
    blocker event). Confirms the bypass is scoped to meta turns only.
    """
    operative = "Limpia el ledger ahora."

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = build_runtime(
                anthropic_executor=_fake_anthropic_returning_start_claim
            )
            runtime.bot.coordinator = None

            response, events = _drive(runtime.bot, operative)

    records = runtime.task_ledger.list(session_id="tg-smoke", limit=20)
    runtimes = [getattr(record, "runtime", "") for record in records]
    assert "evidence_gate" in runtimes, (
        f"non-meta operator turn should still create evidence_gate row; got {runtimes}"
    )

    assert response is not None
    # The original brain content must be replaced by the blocker template.
    assert _BRAIN_START_CLAIM not in response
    assert "Bloqueé" in response

    types = _event_types(events)
    assert "evidence_gate_blocked_start_claim" in types
    assert "evidence_gate_skipped_meta" not in types


# ---------------------------------------------------------------------------
# asyncio.to_thread variants — bloquean por test el invariante
# `evidence_gate_meta_skip_sync_path` (INTERNAL_WIRING §1). Si alguien convierte
# handle_text / _brain_text_response / _prepare_visible_brain_content en
# `async def`, el `with meta_introspection_context(...)` resetea el ContextVar
# antes de que el evidence-gate lo lea y estos tests deben fallar ruidoso.
# ---------------------------------------------------------------------------


def _drive_via_asyncio_to_thread(
    bot, text: str, *, session_id: str = "tg-smoke"
) -> tuple[str | None, list[tuple[str, dict]]]:
    """Same as _drive but executes bot.handle_text inside asyncio.to_thread.

    Mirrors the production Telegram path: telegram.py:1010 does
    `await asyncio.to_thread(self._handle_agent_text_sync, ...)`, which
    dispatches to a default-executor worker thread without a running event
    loop. ContextVar set in that thread must remain visible to the gate
    reader on the same thread.
    """
    captured: list[tuple[str, dict]] = []
    real_emit = bot.observe.emit

    def spy(event_type: str, **kwargs):
        captured.append((event_type, dict(kwargs.get("payload") or {})))
        return real_emit(event_type, **kwargs)

    def _call() -> str | None:
        return bot.handle_text(
            user_id="123",
            session_id=session_id,
            text=text,
            runtime_channel="telegram",
        )

    async def _runner() -> str | None:
        return await asyncio.to_thread(_call)

    with patch.object(bot.observe, "emit", side_effect=spy):
        response = asyncio.run(_runner())
    return response, captured


def test_complaint_no_evidence_gate_task_via_asyncio_to_thread() -> None:
    """Same aserts as test_complaint_no_evidence_gate_task but exercised
    through asyncio.run(asyncio.to_thread(bot.handle_text, ...)) so the
    ContextVar lives entirely inside a worker thread spawned by asyncio.
    Guards the same-thread/sync invariant from INTERNAL_WIRING §1.
    """
    complaint = "¿por qué no completas las tareas que te pido?"

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = build_runtime(
                anthropic_executor=_fake_anthropic_returning_start_claim
            )
            runtime.bot.coordinator = None

            response, events = _drive_via_asyncio_to_thread(runtime.bot, complaint)

    records = runtime.task_ledger.list(session_id="tg-smoke", limit=20)
    runtimes = [getattr(record, "runtime", "") for record in records]
    assert "evidence_gate" not in runtimes, (
        f"meta turn through asyncio.to_thread should not create evidence_gate row; "
        f"got runtimes={runtimes}. If this fires, the ContextVar reset before "
        f"_prepare_visible_brain_content read it — check that handle_text → "
        f"_brain_text_response → _prepare_visible_brain_content stay sync def."
    )

    assert response is not None
    forbidden_fragments = (
        "explicit_blocker",
        "Bloqueé el arranque",
        "Bloqueé esa respuesta",
        "evidence-gate:",
        "Task: `tg-",
    )
    for fragment in forbidden_fragments:
        assert fragment not in response, (
            f"meta-turn response leaked forbidden fragment {fragment!r}: {response!r}"
        )
    assert _BRAIN_START_CLAIM.strip() in response

    types = _event_types(events)
    assert "meta_introspection_routed_to_chat" in types
    assert "evidence_gate_skipped_meta" in types
    assert "evidence_gate_blocked_start_claim" not in types
    assert "evidence_gate_explicit_blocker_recorded" not in types

    skipped = [payload for name, payload in events if name == "evidence_gate_skipped_meta"]
    assert skipped
    assert skipped[0].get("meta_kind") == "meta"
    assert skipped[0].get("reason") == "start_claim_without_evidence"
    assert skipped[0].get("session_id") == "tg-smoke"


def test_non_meta_operator_request_still_trips_evidence_gate_via_asyncio_to_thread() -> None:
    """Counterfactual through asyncio.to_thread: a non-meta operator turn
    must still hit the evidence-gate even when the path runs inside the
    default-executor thread. Confirms the bypass is meta-scoped, not
    accidentally permanent under threaded dispatch.
    """
    operative = "Limpia el ledger ahora."

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, _runtime_env(root), clear=False):
            runtime = build_runtime(
                anthropic_executor=_fake_anthropic_returning_start_claim
            )
            runtime.bot.coordinator = None

            response, events = _drive_via_asyncio_to_thread(runtime.bot, operative)

    records = runtime.task_ledger.list(session_id="tg-smoke", limit=20)
    runtimes = [getattr(record, "runtime", "") for record in records]
    assert "evidence_gate" in runtimes, (
        f"non-meta operator turn through asyncio.to_thread should keep "
        f"the gate armed; got runtimes={runtimes}"
    )

    assert response is not None
    assert _BRAIN_START_CLAIM not in response
    assert "Bloqueé" in response

    types = _event_types(events)
    assert "evidence_gate_blocked_start_claim" in types
    assert "evidence_gate_skipped_meta" not in types
