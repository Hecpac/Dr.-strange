from __future__ import annotations

from claw_v2 import notebooklm_cdp
from claw_v2.notebooklm_cdp import (
    DR_STATE_COMPLETED,
    DR_STATE_IN_PROGRESS,
    DR_STATE_READY_TO_IMPORT,
    DR_STATE_SUBMITTABLE,
    DR_STATE_UNKNOWN,
    _parse_source_count,
    detect_deep_research_state,
)


def test_parse_source_count_from_spanish_and_english_notebook_text() -> None:
    assert _parse_source_count("Fuentes\n(0)\nAgregar fuente") == 0
    assert _parse_source_count("Fuentes (12)\nGuía del usuario") == 12
    assert _parse_source_count("Sources\n7\nAdd source") == 7


def test_parse_source_count_returns_none_without_sources_section() -> None:
    assert _parse_source_count("Iniciemos tu cuaderno\nSube una fuente") is None


# -- Deep Research state detector regression tests --------------------------------
#
# The bug these guard against: on 2026-05-18 a series of NotebookLM runs were
# misdiagnosed as "Deep Research did not persist" because the launcher script
# inspected the source-discovery textarea, found it disabled with aria-label
# "Descubrir fuentes con base en la consulta enviada" (past tense), and assumed
# submission had failed. The query was actually already running OR already
# finished and only waiting on the Importar click. Re-submitting in that state
# either fails silently or destroys completed work.
#
# The detector classifies these states deterministically from the DOM so the
# `deep_research()` entrypoint can short-circuit on prior runs instead of
# blowing them away.


class _FakeLocator:
    def __init__(self, *, visible=False, text="", disabled=False):
        self._visible = visible
        self._text = text
        self._disabled = disabled

    @property
    def first(self):
        return self

    def is_visible(self, timeout=None):
        return self._visible

    def inner_text(self, timeout=None):
        return self._text

    def evaluate(self, expr):
        return self._disabled


class _FakePage:
    def __init__(self, *, body_text: str, textarea=None, importar=None):
        self._body_text = body_text
        self._textarea = textarea or _FakeLocator(visible=False)
        self._importar = importar or _FakeLocator(visible=False)

    def locator(self, selector: str):
        sel = selector.lower()
        if "body" in sel:
            return _FakeLocator(visible=True, text=self._body_text)
        if "consulta enviada" in sel or "textarea" in sel:
            return self._textarea
        if "importar" in sel:
            return self._importar
        return _FakeLocator(visible=False)


def test_detect_completed_when_source_count_present() -> None:
    page = _FakePage(body_text="Fuentes (41)\nDeep Research")
    assert detect_deep_research_state(page) == DR_STATE_COMPLETED


def test_detect_ready_to_import_when_finalizo_marker_present() -> None:
    page = _FakePage(body_text="Iniciemos tu cuaderno\nDeep Research finalizó la búsqueda\nImportar")
    assert detect_deep_research_state(page) == DR_STATE_READY_TO_IMPORT


def test_detect_in_progress_when_planificando_marker_present() -> None:
    page = _FakePage(body_text="Planificando... No salgas de esta página")
    assert detect_deep_research_state(page) == DR_STATE_IN_PROGRESS


def test_detect_in_progress_when_textarea_disabled_with_consulta_enviada() -> None:
    # The 2026-05-18 trap: disabled textarea + past-tense aria-label means a
    # query is already in flight, NOT that submission failed.
    page = _FakePage(
        body_text="Iniciemos tu cuaderno",  # welcome heading still showing
        textarea=_FakeLocator(visible=True, disabled=True),
    )
    assert detect_deep_research_state(page) == DR_STATE_IN_PROGRESS


def test_detect_ready_to_import_when_only_importar_button_visible() -> None:
    page = _FakePage(
        body_text="Some notebook chrome without explicit finalizó text",
        importar=_FakeLocator(visible=True),
    )
    assert detect_deep_research_state(page) == DR_STATE_READY_TO_IMPORT


def test_detect_submittable_on_empty_notebook_welcome() -> None:
    page = _FakePage(body_text="Iniciemos tu cuaderno\nSube una fuente")
    assert detect_deep_research_state(page) == DR_STATE_SUBMITTABLE


def test_detect_unknown_when_no_markers_match() -> None:
    page = _FakePage(body_text="(some unrelated chrome text)")
    assert detect_deep_research_state(page) == DR_STATE_UNKNOWN

