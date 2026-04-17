from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from claw_v2.bot_commands import BotCommand, CommandContext

logger = logging.getLogger(__name__)


class WikiHandler:
    def __init__(self, wiki: Any | None = None, memory: Any | None = None) -> None:
        self.wiki = wiki
        self.memory = memory
        self._ingest_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wiki-ingest")

    def commands(self) -> list[BotCommand]:
        return [
            BotCommand(
                "wiki",
                self.handle_command,
                exact=("/wiki", "/wiki lint"),
                prefixes=("/wiki ingest ", "/wiki query "),
            ),
        ]

    def handle_command(self, context: CommandContext) -> str:
        stripped = context.stripped
        if stripped == "/wiki":
            return self._stats_response()
        if stripped == "/wiki lint":
            return self._lint_response()
        if stripped.startswith("/wiki ingest "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /wiki ingest <title>"
            return self._ingest_response(parts[2], context.session_id)
        parts = stripped.split(maxsplit=2)
        if len(parts) != 3:
            return "usage: /wiki query <question>"
        return self._query_response(parts[2])

    def maybe_ingest(self, title: str, content: str, *, source_type: str = "article") -> None:
        if self.wiki is None or not content or len(content) < 100:
            return
        def _do_ingest():
            try:
                result = self.wiki.ingest(title, content, source_type=source_type)
                logger.info(
                    "Wiki auto-ingest '%s': %d pages written", title, result["pages_written"]
                )
            except Exception:
                logger.debug("Wiki auto-ingest failed for '%s'", title, exc_info=True)
        self._ingest_pool.submit(_do_ingest)

    def _stats_response(self) -> str:
        if self.wiki is None:
            return "wiki service not available"
        stats = self.wiki.stats()
        return (
            f"Wiki: {stats['wiki_pages']} pages, {stats['raw_sources']} raw sources\n"
            f"Root: {stats['wiki_root']}"
        )

    def _lint_response(self) -> str:
        if self.wiki is None:
            return "wiki service not available"
        result = self.wiki.lint()
        parts = [f"Pages: {result.get('total_pages', 0)}, Issues: {result['issues']}"]
        if result["orphans"]:
            parts.append(f"Orphans: {', '.join(result['orphans'][:10])}")
        if result["missing"]:
            parts.append(f"Missing: {', '.join(result['missing'][:10])}")
        return "\n".join(parts)

    def _ingest_response(self, title: str, session_id: str) -> str:
        if self.wiki is None:
            return "wiki service not available"
        if self.memory is None:
            return "memory service not available"
        recent = self.memory.get_recent_messages(session_id, limit=2)
        content = ""
        for msg in reversed(recent):
            if msg.get("role") == "assistant" and msg.get("content"):
                content = msg["content"]
                break
        if not content:
            return "No recent content to ingest. Send content first, then /wiki ingest <title>"
        result = self.wiki.ingest(title, content)
        return (
            f"Ingested: {title}\n"
            f"Pages written: {result['pages_written']}, Updates: {result['updates']}, New: {result['new_pages']}"
        )

    def _query_response(self, question: str) -> str:
        if self.wiki is None:
            return "wiki service not available"
        answer = self.wiki.query(question, archive=True)
        return answer or "No relevant information found in the wiki."
