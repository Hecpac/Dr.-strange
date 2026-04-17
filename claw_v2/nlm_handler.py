from __future__ import annotations

import logging
from typing import Any, Callable

from claw_v2.bot_commands import BotCommand, CommandContext
from claw_v2.bot_helpers import _extract_nlm_artifact_kind, _extract_nlm_create_topic

logger = logging.getLogger(__name__)


class NlmHandler:
    def __init__(
        self,
        update_session_state: Callable[..., None] | None = None,
    ) -> None:
        self.notebooklm: Any | None = None
        self._active_notebooks: dict[str, dict[str, str]] = {}
        self._update_session_state = update_session_state or (lambda *a, **kw: None)

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
        if topic := _extract_nlm_create_topic(text):
            return self._create_response(session_id, topic)
        if kind := _extract_nlm_artifact_kind(text):
            target = self._active_notebook_id(session_id)
            if target is None:
                return "No hay cuaderno activo. Primero dime `creame un cuaderno sobre ...`."
            self._set_active_notebook(session_id, target)
            return self.notebooklm.start_artifact(target, kind)
        return None

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
            return "No hay cuaderno activo. Primero dime `creame un cuaderno sobre ...`."
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
        if notebook is None:
            return None
        return notebook["id"]
