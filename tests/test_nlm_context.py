from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import Any

from claw_v2.nlm_context import NotebookContext, resolve_latest_notebook_context
from claw_v2.nlm_handler import NlmHandler


@dataclass
class _FakeTaskRecord:
    task_id: str
    task_kind: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)


class _FakeTaskLedger:
    def __init__(self, records: list[_FakeTaskRecord]) -> None:
        self._records = records

    def list(self, *, session_id: str, limit: int = 20) -> list[_FakeTaskRecord]:
        return list(self._records)


class _FakeBackend:
    def __init__(self, notebooks: list[dict[str, Any]]) -> None:
        self._notebooks = notebooks
        self.list_calls = 0

    def list_notebooks(self) -> list[dict[str, Any]]:
        self.list_calls += 1
        return self._notebooks


class ResolveContextTests(unittest.TestCase):
    def test_resolves_from_active_object(self) -> None:
        def get_state(_: str) -> dict[str, Any]:
            return {"active_object": {"kind": "notebook", "id": "nb-1", "title": "X"}}

        ctx = resolve_latest_notebook_context(
            session_id="s1",
            get_session_state=get_state,
        )
        self.assertTrue(ctx.found)
        self.assertEqual(ctx.notebook_id, "nb-1")
        self.assertEqual(ctx.source, "active_object")
        self.assertEqual(ctx.confidence, "high")

    def test_resolves_from_task_ledger_artifacts(self) -> None:
        ledger = _FakeTaskLedger(
            [
                _FakeTaskRecord(
                    task_id="t1",
                    task_kind="notebooklm_create",
                    artifacts={"notebook_id": "nb-2", "notebook_title": "Y"},
                ),
            ]
        )
        ctx = resolve_latest_notebook_context(
            session_id="s1",
            get_session_state=lambda _: {"active_object": {}},
            task_ledger=ledger,
        )
        self.assertTrue(ctx.found)
        self.assertEqual(ctx.notebook_id, "nb-2")
        self.assertEqual(ctx.source, "task_ledger")

    def test_resolves_from_backend_when_state_empty(self) -> None:
        backend = _FakeBackend(
            [
                {"id": "nb-old", "title": "Old", "created_at": "2025-01-01"},
                {"id": "nb-new", "title": "New", "created_at": "2026-04-26"},
            ]
        )
        ctx = resolve_latest_notebook_context(
            session_id="s1",
            get_session_state=lambda _: {},
            notebooklm_backend=backend,
        )
        self.assertTrue(ctx.found)
        self.assertEqual(ctx.notebook_id, "nb-new")
        self.assertEqual(ctx.source, "backend")
        self.assertEqual(backend.list_calls, 1)

    def test_backend_multiple_unsorted_returns_low_confidence(self) -> None:
        backend = _FakeBackend(
            [
                {"id": "nb-a", "title": "A"},
                {"id": "nb-b", "title": "B"},
            ]
        )
        ctx = resolve_latest_notebook_context(
            session_id="s1",
            get_session_state=lambda _: {},
            notebooklm_backend=backend,
        )
        self.assertTrue(ctx.found)
        self.assertEqual(ctx.confidence, "low")
        self.assertEqual(ctx.reason, "multiple_candidates")

    def test_backend_single_candidate_high_confidence(self) -> None:
        backend = _FakeBackend([{"id": "nb-only", "title": "Only"}])
        ctx = resolve_latest_notebook_context(
            session_id="s1",
            get_session_state=lambda _: {},
            notebooklm_backend=backend,
        )
        self.assertTrue(ctx.found)
        self.assertEqual(ctx.confidence, "high")

    def test_no_context_returns_actionable_message(self) -> None:
        ctx = resolve_latest_notebook_context(
            session_id="s1",
            get_session_state=lambda _: {},
        )
        self.assertFalse(ctx.found)
        self.assertEqual(ctx.source, "none")

    def test_state_exception_falls_through(self) -> None:
        def boom(_: str) -> dict[str, Any]:
            raise RuntimeError("db down")

        ctx = resolve_latest_notebook_context(
            session_id="s1",
            get_session_state=boom,
        )
        self.assertFalse(ctx.found)


class NlmHandlerWithResolverTests(unittest.TestCase):
    def test_active_notebook_id_falls_back_to_state(self) -> None:
        def get_state(_: str) -> dict[str, Any]:
            return {
                "active_object": {"kind": "notebook", "id": "nb-99", "title": "Z"}
            }

        handler = NlmHandler(get_session_state=get_state)
        self.assertEqual(handler._active_notebook_id("s1"), "nb-99")
        # Subsequent calls hit in-memory cache.
        self.assertEqual(handler._active_notebook_id("s1"), "nb-99")

    def test_missing_notebook_response_uses_resolver(self) -> None:
        backend = _FakeBackend([{"id": "nb-fallback", "title": "Fallback"}])
        handler = NlmHandler(get_session_state=lambda _: {})
        handler.notebooklm = backend
        message = handler._missing_notebook_response("s1")
        self.assertIn("encontré el último registrado", message)
        self.assertIn("Fallback", message)
        # Now that resolver populated active dict, _active_notebook_id finds it.
        self.assertEqual(handler._active_notebook_id("s1"), "nb-fallback")

    def test_missing_notebook_response_returns_default_when_nothing(self) -> None:
        handler = NlmHandler(get_session_state=lambda _: {})

        class _EmptyBackend:
            def list_notebooks(self) -> list:
                return []

        handler.notebooklm = _EmptyBackend()
        message = handler._missing_notebook_response("s1")
        self.assertIn("No hay cuaderno activo", message)

    def test_multiple_candidates_returns_clarifying_message(self) -> None:
        backend = _FakeBackend(
            [
                {"id": "nb-x", "title": "X"},
                {"id": "nb-y", "title": "Y"},
            ]
        )
        handler = NlmHandler(get_session_state=lambda _: {})
        handler.notebooklm = backend
        message = handler._missing_notebook_response("s1")
        self.assertIn("Encontré varios cuadernos recientes", message)


if __name__ == "__main__":
    unittest.main()
