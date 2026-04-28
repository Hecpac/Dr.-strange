"""NotebookLM context resolver.

Antes de responder "No hay cuaderno activo", busca el último notebook
registrado en orden:

  1. session_state.active_object  (si kind == "notebook")
  2. task_ledger artifacts        (último task notebooklm_*)
  3. notebooklm_backend.list_notebooks()
  4. none

Si el backend devuelve >1 candidato cercano sin orden garantizado,
retorna confidence="low" y reason="multiple_candidates" para que el
caller pueda pedir confirmación.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal


NotebookContextSource = Literal[
    "active_object",
    "task_ledger",
    "backend",
    "none",
]


@dataclass(slots=True)
class NotebookContext:
    found: bool
    notebook_id: str | None = None
    notebook_title: str | None = None
    source: NotebookContextSource = "none"
    confidence: str = "low"
    reason: str = ""


_NLM_TASK_KINDS = {"notebooklm_create", "notebooklm_review"}


def _from_active_object(state: dict[str, Any]) -> NotebookContext | None:
    active = state.get("active_object") or {}
    if not isinstance(active, dict):
        return None
    if active.get("kind") != "notebook":
        return None
    notebook_id = active.get("id")
    if not notebook_id:
        return None
    return NotebookContext(
        found=True,
        notebook_id=str(notebook_id),
        notebook_title=str(active.get("title") or ""),
        source="active_object",
        confidence="high",
        reason="active_object_notebook",
    )


def _from_task_ledger(task_ledger: Any, session_id: str) -> NotebookContext | None:
    if task_ledger is None:
        return None
    try:
        records = task_ledger.list(session_id=session_id, limit=20)
    except Exception:
        return None
    for record in records or []:
        intent = None
        metadata = getattr(record, "metadata", None) or {}
        if isinstance(metadata, dict):
            intent = metadata.get("intent")
        kind = getattr(record, "task_kind", None) or intent
        if kind not in _NLM_TASK_KINDS:
            continue
        artifacts = getattr(record, "artifacts", None) or {}
        if not isinstance(artifacts, dict):
            continue
        notebook_id = artifacts.get("notebook_id")
        if not notebook_id:
            continue
        return NotebookContext(
            found=True,
            notebook_id=str(notebook_id),
            notebook_title=str(artifacts.get("notebook_title") or ""),
            source="task_ledger",
            confidence="high",
            reason="task_ledger_artifact",
        )
    return None


def _from_backend(backend: Any) -> NotebookContext | None:
    if backend is None:
        return None
    try:
        notebooks = backend.list_notebooks()
    except Exception:
        return None
    if not notebooks:
        return None
    # Prefer the entry with the most recent timestamp if available.
    candidates = list(notebooks)
    timestamped = [nb for nb in candidates if isinstance(nb, dict) and nb.get("created_at")]
    if timestamped:
        timestamped.sort(key=lambda nb: nb.get("created_at"), reverse=True)
        latest = timestamped[0]
        confidence = "high" if len(timestamped) == 1 else "medium"
        return NotebookContext(
            found=True,
            notebook_id=str(latest.get("id")),
            notebook_title=str(latest.get("title") or ""),
            source="backend",
            confidence=confidence,
            reason="backend_latest_by_timestamp",
        )
    # No timestamps — ambiguous order.
    if len(candidates) == 1:
        only = candidates[0]
        return NotebookContext(
            found=True,
            notebook_id=str(only.get("id")),
            notebook_title=str(only.get("title") or ""),
            source="backend",
            confidence="high",
            reason="backend_single_candidate",
        )
    fallback = candidates[0]
    return NotebookContext(
        found=True,
        notebook_id=str(fallback.get("id")),
        notebook_title=str(fallback.get("title") or ""),
        source="backend",
        confidence="low",
        reason="multiple_candidates",
    )


def resolve_latest_notebook_context(
    *,
    session_id: str,
    get_session_state: Callable[[str], dict[str, Any]] | None = None,
    task_ledger: Any | None = None,
    notebooklm_backend: Any | None = None,
) -> NotebookContext:
    if get_session_state is not None:
        try:
            state = get_session_state(session_id) or {}
        except Exception:
            state = {}
        ctx = _from_active_object(state)
        if ctx is not None:
            return ctx
    ctx = _from_task_ledger(task_ledger, session_id)
    if ctx is not None:
        return ctx
    ctx = _from_backend(notebooklm_backend)
    if ctx is not None:
        return ctx
    return NotebookContext(
        found=False,
        source="none",
        confidence="low",
        reason="no_context_available",
    )
