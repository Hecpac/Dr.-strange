from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from claw_v2.nlm_handler import NlmHandler
from claw_v2.task_ledger import TaskLedger


class _StubNotebookLM:
    def __init__(self, *, raise_on: str | None = None) -> None:
        self._raise_on = raise_on
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def list_notebooks(self) -> list[dict[str, Any]]:
        self.calls.append(("list_notebooks", ()))
        if self._raise_on == "list_notebooks":
            raise RuntimeError("nlm_unreachable")
        return [
            {"id": "nb-old", "title": "Old", "created_at": "2026-04-01T00:00:00Z"},
            {"id": "nb-new", "title": "Recent", "created_at": "2026-04-25T00:00:00Z"},
        ]

    def create_notebook(self, title: str) -> dict[str, Any]:
        self.calls.append(("create_notebook", (title,)))
        if self._raise_on == "create_notebook":
            raise RuntimeError("nlm_create_failed")
        return {"id": "nb-fresh", "title": title}

    def start_research(self, notebook_id: str, query: str) -> str:
        self.calls.append(("start_research", (notebook_id, query)))
        return "research scheduled"

    def chat(self, notebook_id: str, question: str) -> str:
        self.calls.append(("chat", (notebook_id, question)))
        return f"answer for {notebook_id}"


def _build_handler(*, raise_on: str | None = None) -> tuple[NlmHandler, TaskLedger, Path]:
    tmpdir = Path(tempfile.mkdtemp())
    db_path = tmpdir / "claw.db"
    ledger = TaskLedger(db_path)
    handler = NlmHandler(task_ledger=ledger)
    handler.notebooklm = _StubNotebookLM(raise_on=raise_on)
    return handler, ledger, tmpdir


class NotebookTaskCoordinatorContractTests(unittest.TestCase):
    def test_create_notebook_via_natural_language_records_succeeded_task(self) -> None:
        handler, ledger, _ = _build_handler()

        response = handler.natural_language_response(
            "tg-1", "creame un cuaderno sobre arquitectura de agentes"
        )

        self.assertIsNotNone(response)
        records = ledger.list(session_id="tg-1", limit=10)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.runtime, "nlm_natural_language")
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(record.verification_status, "passed")
        self.assertEqual(record.metadata.get("intent"), "create_notebook")
        self.assertTrue(record.task_id.startswith("nlm-"))
        self.assertGreater(len(record.summary), 0)

    def test_review_latest_records_succeeded_task(self) -> None:
        handler, ledger, _ = _build_handler()

        response = handler.natural_language_response(
            "tg-1", "revisa el ultimo cuaderno que creamos"
        )

        self.assertIsNotNone(response)
        records = ledger.list(session_id="tg-1", limit=10)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "succeeded")
        self.assertEqual(records[0].verification_status, "passed")
        self.assertEqual(records[0].metadata.get("intent"), "review_latest")

    def test_failure_during_dispatch_records_failed_task(self) -> None:
        handler, ledger, _ = _build_handler(raise_on="create_notebook")

        response = handler.natural_language_response(
            "tg-1", "creame un cuaderno sobre cosas"
        )

        self.assertIsNotNone(response)
        self.assertTrue(response.startswith("Error"))
        records = ledger.list(session_id="tg-1", limit=10)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "failed")
        self.assertEqual(records[0].verification_status, "failed")
        self.assertIn("nlm_create_failed", records[0].error)

    def test_non_notebook_text_does_not_create_a_ledger_entry(self) -> None:
        handler, ledger, _ = _build_handler()

        response = handler.natural_language_response("tg-1", "hola, como estas?")

        self.assertIsNone(response)
        records = ledger.list(session_id="tg-1", limit=10)
        self.assertEqual(records, [])

    def test_handler_without_task_ledger_still_returns_response(self) -> None:
        handler = NlmHandler()
        handler.notebooklm = _StubNotebookLM()

        response = handler.natural_language_response(
            "tg-1", "creame un cuaderno sobre nada"
        )

        self.assertIsNotNone(response)


class BotWiringTests(unittest.TestCase):
    def test_bot_constructs_nlm_handler_with_task_ledger_kwarg(self) -> None:
        # Static contract: bot.py must pass task_ledger= when constructing NlmHandler,
        # otherwise the audit-trail recording in NlmHandler stays disabled.
        source = Path("claw_v2/bot.py").read_text(encoding="utf-8")
        idx = source.find("NlmHandler(")
        self.assertNotEqual(idx, -1, "bot.py no longer constructs NlmHandler")
        block = source[idx : idx + 400]
        self.assertIn("task_ledger=task_ledger", block)


if __name__ == "__main__":
    unittest.main()
