from __future__ import annotations

from claw_v2.notebooklm_cdp import _parse_source_count


def test_parse_source_count_from_spanish_and_english_notebook_text() -> None:
    assert _parse_source_count("Fuentes\n(0)\nAgregar fuente") == 0
    assert _parse_source_count("Fuentes (12)\nGuía del usuario") == 12
    assert _parse_source_count("Sources\n7\nAdd source") == 7


def test_parse_source_count_returns_none_without_sources_section() -> None:
    assert _parse_source_count("Iniciemos tu cuaderno\nSube una fuente") is None
