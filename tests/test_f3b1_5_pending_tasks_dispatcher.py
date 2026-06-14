"""F3b.1.5 — pending_tasks dispatcher imperative-intent veto.

Regression test of the 2026-05-17 fix: the pending-tasks handler must only
trigger on explicit status queries; imperative commands (procede, ejecuta,
OK final, marca como done, long instruction blocks, etc.) must fall through
to the brain.

100% offline. Autouse `_no_network` fixture blocks sockets/urllib.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("Network call attempted from F3b.1.5 test — forbidden")
    import socket
    import urllib.request
    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    yield


@pytest.fixture
def matcher_only():
    """A minimal harness that calls the matcher methods without booting the
    full bot. We exercise the pure-text logic on a stub class that inherits
    the methods we need from BotService."""
    from claw_v2.bot import BotService

    class _Stub:
        # Borrow methods that don't need state.
        _IMPERATIVE_INTENT_MARKERS = BotService._IMPERATIVE_INTENT_MARKERS
        _has_imperative_intent = BotService._has_imperative_intent
        _pending_tasks_query_matches = BotService._pending_tasks_query_matches

        # F3b.1.5 — task_status_overview is out of scope; stub it.
        def _task_status_overview_query_matches(self, normalized):
            return False
        _emitted: list[dict] = []

        class _FakeObserve:
            emitted: list[dict] = []

            def emit(self, event, *, payload):
                self.emitted.append({"event": event, "payload": payload})

        observe = _FakeObserve()

        def _emit_dispatcher_fallthrough(self, *, source, reason, text):
            self.observe.emit(
                "dispatcher_fallthrough_imperative",
                payload={
                    "source": source,
                    "reason": reason,
                    "text_head": (text or "")[:120],
                    "text_len": len(text or ""),
                },
            )

    return _Stub()


# ===========================================================================
# §1 — Short explicit status queries STILL trigger the handler.
# ===========================================================================


def test_short_status_query_qué_tareas_pendientes(matcher_only):
    assert matcher_only._pending_tasks_query_matches("qué tareas pendientes hay?") is True


def test_short_status_query_muestrame_pendientes(matcher_only):
    """The base matcher accepts "pendientes" compact-form OR "tareas + pendient"
    pair. Either phrasing should still trigger when no imperative is present."""
    # "tareas pendientes" → compact contains both required tokens
    assert matcher_only._pending_tasks_query_matches("muéstrame tareas pendientes") is True


def test_short_status_query_tareas_pendientes_plain(matcher_only):
    assert matcher_only._pending_tasks_query_matches("tareas pendientes") is True


# ===========================================================================
# §2 — Long instruction block + "Procede con esto" → FALLTHROUGH (the bug).
# ===========================================================================


def test_long_block_with_procede_falls_through(matcher_only):
    msg = (
        "OK final: marca F3b.1 como SUCCEEDED.\n\n"
        "Evidence:\n"
        "- artifacts/verification/f3b1_reconcile.log\n"
        "- pytest: 184/184 passed, exit_code=0\n"
        "- zero-network fixture activo\n\n"
        "No ejecutes F3c. No toques X, LinkedIn, browser/CDP.\n"
        "Siguiente fase: F3b.2 con HeyGenDeliver real.\n"
        "Tareas pendientes a confirmar antes de seguir.\n\n"
        "Procede con esto."
    )
    matcher_only.observe.emitted.clear()
    assert matcher_only._pending_tasks_query_matches(msg) is False
    # Fallthrough event recorded
    events = [e for e in matcher_only.observe.emitted
              if e["event"] == "dispatcher_fallthrough_imperative"]
    assert events
    assert events[0]["payload"]["reason"] == "imperative_intent_detected"


# ===========================================================================
# §3 — "OK final: marca F3b.1 como succeeded" → FALLTHROUGH.
# ===========================================================================


def test_ok_final_marca_falls_through(matcher_only):
    msg = "tareas pendientes — OK final: marca F3b.1 como succeeded"
    matcher_only.observe.emitted.clear()
    assert matcher_only._pending_tasks_query_matches(msg) is False
    assert any(e["event"] == "dispatcher_fallthrough_imperative"
               for e in matcher_only.observe.emitted)


# ===========================================================================
# §4 — "aprobado iniciar F3b.2" → FALLTHROUGH.
# ===========================================================================


def test_aprobado_iniciar_falls_through(matcher_only):
    msg = "aprobado iniciar F3b.2 sobre las tareas pendientes"
    assert matcher_only._pending_tasks_query_matches(msg) is False


# ===========================================================================
# §5 — Mixed message "antes de proceder, dime tareas pendientes" — when
# the dominant intent is execution ("proceder"), it should fall through.
# A pure status query with no imperative wins still triggers.
# ===========================================================================


def test_mixed_with_proceder_falls_through(matcher_only):
    """The veto is conservative: any imperative marker triggers fallthrough."""
    msg = "antes de proceder, dime las tareas pendientes"
    assert matcher_only._pending_tasks_query_matches(msg) is False


def test_pure_status_query_no_imperative_still_matches(matcher_only):
    """No imperative markers + base match → handler still triggers."""
    msg = "qué tareas pendientes hay"
    assert matcher_only._pending_tasks_query_matches(msg) is True


# ===========================================================================
# §6 — Regression test for the 2026-05-17 incident.
# ===========================================================================


def test_regression_2026_05_17_long_block_with_continua(matcher_only):
    """Exact pattern observed on 2026-05-17 + recurrence on 2026-05-26:
    long message with task body + 'Continúa' or 'Procede'."""
    msg = (
        "F3a-extension está parcialmente aprobado, pero no lo marques done todavía. "
        "Ejecuta F3a-ext.1 para cerrar bypasses semánticos antes de F3b. "
        "Restricciones: 1. No ejecutes F3b. 2. No toques X, LinkedIn, "
        "deploy, GitHub remoto ni browser/CDP. 3. Cero llamadas externas. "
        "Las tareas pendientes deben enumerarse aquí. Continúa."
    )
    matcher_only.observe.emitted.clear()
    assert matcher_only._pending_tasks_query_matches(msg) is False
    fall_events = [e for e in matcher_only.observe.emitted
                   if e["event"] == "dispatcher_fallthrough_imperative"]
    assert fall_events, "Regression: long imperative block must emit fallthrough event"
    payload = fall_events[0]["payload"]
    assert payload["text_len"] > 100
    assert payload["source"] == "pending_tasks_query_matches"


# ===========================================================================
# §7 — Observability: every veto emits dispatcher_fallthrough_imperative.
# ===========================================================================


def test_every_veto_emits_telemetry(matcher_only):
    cases = [
        "tareas pendientes, procede",
        "tareas pendientes, ejecutalo ya",
        "tareas pendientes, OK final",
        "tareas pendientes, hazlo",
        "tareas pendientes, dale",
        "tareas pendientes, marca como done",
    ]
    for msg in cases:
        matcher_only.observe.emitted.clear()
        assert matcher_only._pending_tasks_query_matches(msg) is False, msg
        assert any(e["event"] == "dispatcher_fallthrough_imperative"
                   for e in matcher_only.observe.emitted), f"no event for: {msg}"


# ===========================================================================
# §8 — Long message (>= 300 chars) is treated as imperative by default,
# even without explicit markers, because status queries are short.
# ===========================================================================


def test_very_long_message_falls_through_even_without_marker(matcher_only):
    msg = "x" * 500 + " tareas pendientes"
    assert matcher_only._pending_tasks_query_matches(msg) is False


# ===========================================================================
# §9 — Sanity: messages that don't even mention pending tasks short-circuit
# before the imperative veto runs (the matcher returns False at the base
# check, NOT via the veto).
# ===========================================================================


def test_non_status_message_doesnt_match_even_if_imperative(matcher_only):
    matcher_only.observe.emitted.clear()
    msg = "procede con el deploy"
    assert matcher_only._pending_tasks_query_matches(msg) is False
    # Veto not triggered because base match was False.
    assert not matcher_only.observe.emitted
