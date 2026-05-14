"""Regression: _looks_like_direct_actionable_task must not match
introspective questions whose tokens happen to be substrings of the
PR-completion vocabulary ("pr" inside "pregunta", "completa" inside
"completas"). The original substring matcher classified

    "Mi pregunta es porque no completas las tareas faciles ..."

as task_kind=pull_request_completion, sending it into the coordinator
which then timed out at 300s in Codex research."""

from __future__ import annotations

from claw_v2.bot import BotService


_FALSE_POSITIVES = (
    "Mi pregunta es porque no completas las tareas faciles o que no necesitan intervencion o permiso ?",
    "Mi pregunta es porque no completas",
    "Por que no completas las cosas?",
    "Cuando termina la reunion de hoy?",
    "Por que no finalizas la nota?",
)

_TRUE_POSITIVES = (
    "termina el PR",
    "completa el PR #25",
    "finaliza el pr de redaction",
    "PR termina",
)


def test_introspective_questions_do_not_match_pr_completion() -> None:
    for text in _FALSE_POSITIVES:
        assert BotService._looks_like_direct_actionable_task(text) is False, text


def test_real_pr_completion_requests_still_match() -> None:
    for text in _TRUE_POSITIVES:
        assert BotService._looks_like_direct_actionable_task(text) is True, text
