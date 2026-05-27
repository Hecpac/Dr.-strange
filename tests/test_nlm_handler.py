from __future__ import annotations

import unittest
from typing import Any

from claw_v2.nlm_handler import NlmHandler


class _FakeNotebookLM:
    def __init__(self) -> None:
        self.created_titles: list[str] = []
        self.podcast_targets: list[str] = []
        self.orchestration_calls: list[tuple[str, str | None]] = []

    def create_notebook(self, title: str) -> dict[str, str]:
        self.created_titles.append(title)
        return {"id": "nb-1", "title": title}

    def start_research(self, notebook_id: str, query: str) -> str:
        return f"Deep Research iniciado para {query} en {notebook_id}"

    def start_podcast(self, notebook_id: str) -> str:
        self.podcast_targets.append(notebook_id)
        return f"Generando podcast para {notebook_id}"

    def start_orchestration(self, notebook_id: str, *, session_id: str | None = None) -> str:
        self.orchestration_calls.append((notebook_id, session_id))
        return f"Orquestación durable de NotebookLM registrada para {notebook_id}"

    def list_notebooks(self) -> list[dict[str, str]]:
        return []


class NlmHandlerContextualTopicTests(unittest.TestCase):
    def test_del_tema_resolves_clear_recent_topic(self) -> None:
        service = _FakeNotebookLM()
        messages = [
            {
                "role": "assistant",
                "content": "Paper MSM — Model Spec Midtraining: análisis del paper de Anthropic.",
            }
        ]
        handler = NlmHandler(
            get_session_state=lambda _session_id: {},
            get_recent_messages=lambda _session_id, _limit: messages,
        )
        handler.notebooklm = service

        response = handler.natural_language_response(
            "s1",
            "Haz un deep research en NotebookLM y un podcast para saber más del tema",
        )

        self.assertIsNotNone(response)
        self.assertIn("Notebook creado", response)
        self.assertIn("Paper MSM", service.created_titles[0])
        self.assertEqual(service.podcast_targets, ["nb-1"])

    def test_del_tema_asks_short_clarification_when_ambiguous(self) -> None:
        service = _FakeNotebookLM()

        def get_state(_session_id: str) -> dict[str, Any]:
            return {
                "active_object": {
                    "recent_topics": [
                        "MSM Model Spec Midtraining",
                        "Petri 2.0 eval-awareness",
                    ]
                }
            }

        handler = NlmHandler(get_session_state=get_state)
        handler.notebooklm = service

        response = handler.natural_language_response(
            "s1",
            "haz un cuaderno en NotebookLM del tema",
        )

        self.assertIsNotNone(response)
        assert response is not None
        self.assertIn("¿De cuál tema", response)
        self.assertEqual(service.created_titles, [])

    def test_monitor_outputs_request_uses_durable_orchestration_job(self) -> None:
        service = _FakeNotebookLM()
        handler = NlmHandler(get_session_state=lambda _session_id: {})
        handler.notebooklm = service
        handler._set_active_notebook("s1", "nb-active", "Active Notebook")

        response = handler.natural_language_response(
            "s1",
            "Monitorea el cuaderno en NotebookLM y cuando termine descarga el podcast e informe",
        )

        self.assertIsNotNone(response)
        self.assertIn("Orquestación durable", response)
        self.assertEqual(service.orchestration_calls, [("nb-active", "s1")])


if __name__ == "__main__":
    unittest.main()
