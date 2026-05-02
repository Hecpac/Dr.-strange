from __future__ import annotations

import os
from datetime import datetime
import logging
import unicodedata
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from claw_v2.bot_commands import BotCommand, CommandContext
from claw_v2.bot_helpers import _extract_nlm_artifact_kind, _extract_nlm_create_topic
from claw_v2.nlm_context import NotebookContext, resolve_latest_notebook_context

logger = logging.getLogger(__name__)


class NlmHandler:
    def __init__(
        self,
        update_session_state: Callable[..., None] | None = None,
        *,
        task_ledger: Any | None = None,
        get_session_state: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.notebooklm: Any | None = None
        self._active_notebooks: dict[str, dict[str, str]] = {}
        self._update_session_state = update_session_state or (lambda *a, **kw: None)
        self._task_ledger = task_ledger
        self._get_session_state = get_session_state

    def commands(self) -> list[BotCommand]:
        return [
            BotCommand(
                "notebooklm",
                self._handle_command,
                prefixes=("/nlm_",),
            ),
        ]

    def _handle_command(self, context: CommandContext) -> str:
        return self.dispatch(context.session_id, context.stripped)

    def natural_language_response(self, session_id: str, text: str) -> str | None:
        if self.notebooklm is None or not text or text.startswith("/"):
            return None
        # Keep an emergency kill switch for false-positive incidents, but the
        # classifier below is narrow enough for explicit NotebookLM requests.
        if os.getenv("CLAW_DISABLE_NLM_NATURAL_LANGUAGE", "0") == "1":
            return None
        intent = self._classify_intent(text)
        if intent is None:
            return None
        with self._record_task(session_id=session_id, objective=text, intent=intent) as recorder:
            try:
                response = self._dispatch_intent(session_id, text, intent)
            except ValueError as exc:
                response = f"Error: {exc}"
                recorder.fail(response)
                return response
            except Exception as exc:
                logger.exception("NLM natural language error")
                response = f"Error en NotebookLM: {exc}"
                recorder.fail(response)
                return response
            if response is None:
                recorder.skip()
                return None
            if response.startswith("Error"):
                recorder.fail(response)
            else:
                recorder.succeed(response, evidence=self._evidence_for_intent(session_id, intent, response))
            return response

    def _evidence_for_intent(self, session_id: str, intent: str, response: str) -> dict[str, Any]:
        notebook = self._active_notebooks.get(session_id) or {}
        evidence: dict[str, Any] = {"handler_result": response[:1000]}
        if notebook.get("id"):
            evidence["notebook_id"] = notebook["id"]
        if notebook.get("title"):
            evidence["notebook_title"] = notebook["title"]
        if intent == "review_latest":
            evidence["review_summary"] = response[:1000]
        return evidence

    def _classify_intent(self, text: str) -> str | None:
        if _looks_like_recent_notebook_review(text):
            return "review_latest"
        if _extract_nlm_create_topic(text):
            return "create_notebook"
        if _extract_nlm_artifact_kind(text):
            return "start_artifact"
        return None

    def _dispatch_intent(self, session_id: str, text: str, intent: str) -> str | None:
        if intent == "review_latest":
            return self._review_latest_response(session_id)
        if intent == "create_notebook":
            topic = _extract_nlm_create_topic(text)
            if topic is None:
                return None
            return self._create_response(session_id, topic)
        if intent == "start_artifact":
            kind = _extract_nlm_artifact_kind(text)
            if kind is None:
                return None
            target = self._active_notebook_id(session_id)
            if target is None:
                return self._missing_notebook_response(session_id)
            self._set_active_notebook(session_id, target)
            return self.notebooklm.start_artifact(target, kind)
        return None

    def _missing_notebook_response(self, session_id: str) -> str:
        ctx = self._resolve_notebook_context(session_id)
        if not ctx.found:
            return "No hay cuaderno activo. Primero dime `creame un cuaderno sobre ...`."
        title = ctx.notebook_title or (ctx.notebook_id or "")[:8]
        if ctx.notebook_id:
            self._set_active_notebook(session_id, ctx.notebook_id, ctx.notebook_title)
        if ctx.confidence == "low" and ctx.reason == "multiple_candidates":
            return (
                f"Encontré varios cuadernos recientes; usaré el más reciente registrado: {title}."
            )
        return (
            "No veo cuaderno activo en esta sesión, pero encontré el último registrado: "
            f"{title}. Lo usaré para esta acción."
        )

    @contextmanager
    def _record_task(
        self,
        *,
        session_id: str,
        objective: str,
        intent: str,
    ) -> Iterator["_NlmTaskRecorder"]:
        recorder = _NlmTaskRecorder(self._task_ledger)
        if self._task_ledger is None:
            yield recorder
            return
        task_id = f"nlm-{uuid.uuid4().hex[:12]}"
        try:
            self._task_ledger.create(
                task_id=task_id,
                session_id=session_id,
                objective=objective[:500],
                runtime="nlm_natural_language",
                status="running",
                metadata={"intent": intent},
            )
            recorder.bind(task_id, intent=intent)
        except Exception:
            logger.debug("nlm task ledger create failed", exc_info=True)
        try:
            yield recorder
        finally:
            recorder.finalize_if_pending()

    def dispatch(self, session_id: str, command: str) -> str:
        if self.notebooklm is None:
            return "NotebookLM no disponible. El servicio no está configurado."
        try:
            if command == "/nlm_list":
                return self._list_response()
            if command.startswith("/nlm_create "):
                title = command.split(maxsplit=1)[1]
                return self._create_response(session_id, title)
            if command == "/nlm_create":
                return "usage: /nlm_create <titulo>"
            if command.startswith("/nlm_delete "):
                nb_id = command.split(maxsplit=1)[1]
                return self._delete_response(nb_id)
            if command == "/nlm_delete":
                return "usage: /nlm_delete <notebook_id>"
            if command.startswith("/nlm_status "):
                nb_id = command.split(maxsplit=1)[1]
                return self._status_response(nb_id)
            if command == "/nlm_status":
                return "usage: /nlm_status <notebook_id>"
            if command.startswith("/nlm_sources "):
                parts = command.split()
                if len(parts) < 3:
                    return "usage: /nlm_sources <notebook_id> <url1> [url2] ..."
                return self._sources_response(parts[1], parts[2:])
            if command == "/nlm_sources":
                return "usage: /nlm_sources <notebook_id> <url1> [url2] ..."
            if command.startswith("/nlm_text "):
                rest = command.split(maxsplit=2)
                if len(rest) < 3 or "|" not in rest[2]:
                    return "usage: /nlm_text <notebook_id> <titulo> | <contenido>"
                nb_id = rest[1]
                title_and_content = rest[2]
                pipe_idx = title_and_content.index("|")
                title = title_and_content[:pipe_idx].strip()
                content = title_and_content[pipe_idx + 1:].strip()
                return self._text_response(nb_id, title, content)
            if command == "/nlm_text":
                return "usage: /nlm_text <notebook_id> <titulo> | <contenido>"
            if command.startswith("/nlm_research "):
                parts = command.split(maxsplit=2)
                if len(parts) < 3:
                    return "usage: /nlm_research <notebook_id> <query>"
                return self._research_response(parts[1], parts[2])
            if command == "/nlm_research":
                return "usage: /nlm_research <notebook_id> <query>"
            if command.startswith("/nlm_podcast "):
                nb_id = command.split(maxsplit=1)[1]
                return self._podcast_response(session_id, nb_id)
            if command == "/nlm_podcast":
                return self._podcast_response(session_id, None)
            if command.startswith("/nlm_chat "):
                parts = command.split(maxsplit=2)
                if len(parts) < 3:
                    return "usage: /nlm_chat <notebook_id> <pregunta>"
                return self._chat_response(parts[1], parts[2])
            if command == "/nlm_chat":
                return "usage: /nlm_chat <notebook_id> <pregunta>"
            return "Comando NLM no reconocido. Disponibles: /nlm_list, /nlm_create, /nlm_delete, /nlm_status, /nlm_sources, /nlm_text, /nlm_research, /nlm_podcast, /nlm_chat"
        except ValueError as exc:
            return f"Error: {exc}"
        except Exception as exc:
            logger.exception("NLM command error")
            return f"Error en NotebookLM: {exc}"

    def _list_response(self) -> str:
        notebooks = self.notebooklm.list_notebooks()
        if not notebooks:
            return "No hay notebooks."
        lines = []
        for nb in notebooks:
            short_id = nb["id"][:8]
            date = nb.get("created_at") or "-"
            lines.append(f"{short_id}  {nb['title']}  {date}")
        return "\n".join(lines)

    def _create_response(self, session_id: str, title: str) -> str:
        result = self.notebooklm.create_notebook(title)
        self._set_active_notebook(session_id, result["id"], result["title"])
        research = self.notebooklm.start_research(result["id"], result["title"])
        return (
            f"Notebook creado: {result['id'][:8]} — {result['title']}\n"
            f"{research}\n"
            "Queda como cuaderno activo para esta conversación."
        )

    def _review_latest_response(self, session_id: str) -> str:
        notebooks = self.notebooklm.list_notebooks()
        if not notebooks:
            return "No hay notebooks para revisar."
        latest = _latest_notebook(notebooks)
        nb_id = str(latest["id"])
        title = str(latest.get("title") or nb_id[:8])
        self._set_active_notebook(session_id, nb_id, title)
        question = (
            "Revisa este cuaderno y responde en espanol con: "
            "1) resumen ejecutivo, 2) hallazgos clave, "
            "3) fuentes o evidencias importantes, 4) riesgos o huecos, "
            "5) proximos pasos accionables."
        )
        answer = self.notebooklm.chat(nb_id, question)
        return f"Revision del ultimo cuaderno: {title} ({nb_id[:8]})\n\n{answer}"

    def _delete_response(self, notebook_id: str) -> str:
        self.notebooklm.delete_notebook(notebook_id)
        return "Notebook eliminado."

    def _status_response(self, notebook_id: str) -> str:
        info = self.notebooklm.status(notebook_id)
        nb = info["notebook"]
        lines = [f"Notebook: {nb['title']} ({nb['id'][:8]})", f"Sources: {nb['sources_count']}"]
        for src in info["sources"]:
            lines.append(f"  - {src['title']} [{src['kind']}]")
        return "\n".join(lines)

    def _sources_response(self, notebook_id: str, urls: list[str]) -> str:
        results = self.notebooklm.add_sources(notebook_id, urls)
        lines = [f"{len(results)} source(s) agregados:"]
        for src in results:
            lines.append(f"  - {src['title']}")
        return "\n".join(lines)

    def _text_response(self, notebook_id: str, title: str, content: str) -> str:
        if not title.strip() or not content.strip():
            return "usage: /nlm_text <notebook_id> <titulo> | <contenido>"
        result = self.notebooklm.add_text(notebook_id, title, content)
        return f"Source de texto agregado: {result['title']}"

    def _research_response(self, notebook_id: str, query: str) -> str:
        return self.notebooklm.start_research(notebook_id, query)

    def _podcast_response(self, session_id: str, notebook_id: str | None) -> str:
        target = notebook_id or self._active_notebook_id(session_id)
        if target is None:
            return self._missing_notebook_response(session_id)
        self._set_active_notebook(session_id, target)
        return self.notebooklm.start_podcast(target)

    def _chat_response(self, notebook_id: str, question: str) -> str:
        return self.notebooklm.chat(notebook_id, question)

    def _set_active_notebook(self, session_id: str, notebook_id: str, title: str | None = None) -> None:
        current = self._active_notebooks.get(session_id, {})
        notebook = {
            "id": notebook_id,
            "title": title or current.get("title", notebook_id[:8]),
        }
        self._active_notebooks[session_id] = notebook
        self._update_session_state(
            session_id,
            mode="research",
            active_object={"kind": "notebook", **notebook},
        )

    def _active_notebook_id(self, session_id: str) -> str | None:
        notebook = self._active_notebooks.get(session_id)
        if notebook is not None:
            return notebook["id"]
        # Fallback: read persisted active_object from session state
        if self._get_session_state is not None:
            try:
                state = self._get_session_state(session_id) or {}
            except Exception:
                state = {}
            active = state.get("active_object") or {}
            if isinstance(active, dict) and active.get("kind") == "notebook":
                notebook_id = active.get("id")
                title = active.get("title")
                if notebook_id:
                    self._active_notebooks[session_id] = {
                        "id": str(notebook_id),
                        "title": str(title or notebook_id)[:40],
                    }
                    return str(notebook_id)
        return None

    def _resolve_notebook_context(self, session_id: str) -> NotebookContext:
        return resolve_latest_notebook_context(
            session_id=session_id,
            get_session_state=self._get_session_state,
            task_ledger=self._task_ledger,
            notebooklm_backend=self.notebooklm,
        )


_NLM_INTENT_TO_TASK_KIND: dict[str, str] = {
    "create_notebook": "notebooklm_create",
    "review_latest": "notebooklm_review",
}


class _NlmTaskRecorder:
    def __init__(self, task_ledger: Any | None) -> None:
        self._task_ledger = task_ledger
        self._task_id: str | None = None
        self._intent: str | None = None
        self._finalized = False

    def bind(self, task_id: str, *, intent: str | None = None) -> None:
        self._task_id = task_id
        self._intent = intent

    def succeed(self, summary: str, *, evidence: dict[str, Any] | None = None) -> None:
        from claw_v2.verification_profiles import verify_profile_evidence

        evidence_dict = dict(evidence or {"handler_result": summary[:1000]})
        task_kind = _NLM_INTENT_TO_TASK_KIND.get(self._intent or "")
        if task_kind is not None:
            decision = verify_profile_evidence(task_kind=task_kind, evidence=evidence_dict)
            if decision.status == "passed":
                self._mark(
                    "succeeded",
                    verification_status="passed",
                    summary=summary[:1000],
                    artifacts=evidence_dict,
                )
                return
            self._mark(
                "succeeded" if decision.status == "blocked" else "failed",
                verification_status=decision.status,
                summary=summary[:1000],
                artifacts=evidence_dict,
                error=";".join(decision.missing_evidence) if decision.missing_evidence else "",
            )
            return
        self._mark(
            "succeeded",
            verification_status="passed",
            summary=summary[:1000],
            artifacts=evidence_dict,
        )

    def fail(self, error: str) -> None:
        self._mark("failed", verification_status="failed", error=error[:1000])

    def skip(self) -> None:
        self._mark("cancelled", verification_status="skipped", summary="no_op")

    def finalize_if_pending(self) -> None:
        if self._finalized:
            return
        self._mark("failed", verification_status="failed", error="task_finalized_without_outcome")

    def _mark(
        self,
        status: str,
        *,
        verification_status: str,
        summary: str = "",
        error: str = "",
        artifacts: dict[str, Any] | None = None,
    ) -> None:
        if self._finalized or self._task_ledger is None or self._task_id is None:
            self._finalized = True
            return
        try:
            self._task_ledger.mark_terminal(
                self._task_id,
                status=status,
                summary=summary,
                error=error,
                verification_status=verification_status,
                artifacts=artifacts,
            )
        except Exception:
            logger.debug("nlm task ledger mark_terminal failed", exc_info=True)
        self._finalized = True


def _looks_like_recent_notebook_review(text: str) -> bool:
    normalized = _normalize(text)
    if not any(token in normalized for token in ("notebooklm", "notebook", "cuaderno")):
        return False
    if not any(token in normalized for token in ("revisa", "review", "analiza", "audita", "check")):
        return False
    return any(token in normalized for token in ("ultimo", "reciente", "creado", "latest", "last"))


def _latest_notebook(notebooks: list[dict[str, Any]]) -> dict[str, Any]:
    indexed = list(enumerate(notebooks))
    return max(indexed, key=lambda item: (_created_at_timestamp(item[1]), -item[0]))[1]


def _created_at_timestamp(notebook: dict[str, Any]) -> float:
    raw = notebook.get("created_at")
    if not raw:
        return 0.0
    text = str(raw).strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))
