"""F3b.1.5.1 — fix substring false positives in imperative-intent detection.

The previous `go` marker matched substrings like "tengo", "luego", "algo",
breaking legitimate Spanish status queries. F3b.1.5.1 drops `go` as a
standalone marker, keeps `go ahead` (unambiguous compound), and uses
word-boundary matching for any marker <= 3 chars.

100% offline. Autouse `_no_network` fixture.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("Network call attempted from F3b.1.5.1 — forbidden")

    import socket
    import urllib.request

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    yield


@pytest.fixture
def matcher_only():
    from claw_v2.bot import BotService

    class _Stub:
        _IMPERATIVE_INTENT_MARKERS = BotService._IMPERATIVE_INTENT_MARKERS
        _has_imperative_intent = BotService._has_imperative_intent
        _pending_tasks_query_matches = BotService._pending_tasks_query_matches

        def _task_status_overview_query_matches(self, normalized):
            return False

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
                    "text_head": text[:120],
                    "text_len": len(text),
                },
            )

    return _Stub()


# ===========================================================================
# §1-4 — status queries that previously false-positive on "go" via tengo/luego/algo
# ===========================================================================


def test_que_tengo_pendiente_is_status(matcher_only):
    """tengo contains 'go' substring — must NOT be classified imperative."""
    assert matcher_only._has_imperative_intent("qué tengo pendiente?") is False


def test_que_tareas_tengo_pendientes_is_status(matcher_only):
    msg = "qué tareas tengo pendientes?"
    assert matcher_only._has_imperative_intent(msg) is False
    assert matcher_only._pending_tasks_query_matches(msg) is True


def test_hay_algo_pendiente_is_status(matcher_only):
    """algo contains 'go' substring."""
    assert matcher_only._has_imperative_intent("hay algo pendiente?") is False


def test_luego_dime_tareas_pendientes_is_status(matcher_only):
    """luego contains 'go' substring."""
    msg = "luego dime tareas pendientes"
    assert matcher_only._has_imperative_intent(msg) is False
    assert matcher_only._pending_tasks_query_matches(msg) is True


# ===========================================================================
# §5 — "go ahead" still triggers as imperative (compound, no ambiguity)
# ===========================================================================


def test_go_ahead_is_imperative(matcher_only):
    msg = "go ahead, ejecuta F3b.2 sobre las tareas pendientes"
    assert matcher_only._has_imperative_intent(msg) is True
    assert matcher_only._pending_tasks_query_matches(msg) is False


# ===========================================================================
# §6 — "OK final" still imperative
# ===========================================================================


def test_ok_final_is_still_imperative(matcher_only):
    msg = "OK final: marca F3b.1.5 como succeeded — tareas pendientes ya confirmadas"
    assert matcher_only._has_imperative_intent(msg) is True
    assert matcher_only._pending_tasks_query_matches(msg) is False


# ===========================================================================
# Sanity — other Spanish words containing "go" as substring don't false-positive.
# ===========================================================================


@pytest.mark.parametrize(
    "word",
    [
        "pago",
        "trago",
        "fuego",
        "amigo",
        "domingo",
        "estoy haciendo algo",
        "tengo dudas",
        "luego de eso",
        "agosto",
        "rasgo",
        "diálogo",
    ],
)
def test_words_containing_go_substring_are_not_imperative(matcher_only, word):
    msg = f"qué tareas pendientes {word}"
    # Base matcher still wants "tareas + pendient" → matches.
    # The veto must NOT fire because "go" alone is no longer a marker.
    assert matcher_only._has_imperative_intent(msg) is False
    assert matcher_only._pending_tasks_query_matches(msg) is True


# ===========================================================================
# Defensive — confirm "go" is no longer in the marker set
# ===========================================================================


def test_go_is_not_a_standalone_marker(matcher_only):
    assert "go" not in matcher_only._IMPERATIVE_INTENT_MARKERS
    assert "go ahead" in matcher_only._IMPERATIVE_INTENT_MARKERS
