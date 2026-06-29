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
                exact=("/wiki", "/wiki lint", "/wiki quality", "/wiki research"),
                prefixes=("/wiki ingest ", "/wiki query ", "/wiki research "),
            ),
        ]

    def handle_command(self, context: CommandContext) -> str:
        stripped = context.stripped
        if stripped == "/wiki":
            return self._stats_response()
        if stripped == "/wiki lint":
            return self._lint_response()
        if stripped == "/wiki quality":
            return self._quality_response()
        if stripped == "/wiki research":
            return self._research_response()
        if stripped.startswith("/wiki research "):
            parts = stripped.split(maxsplit=2)
            status = parts[2].strip() if len(parts) == 3 else ""
            return self._research_response(status=status or None)
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

    def _quality_response(self) -> str:
        if self.wiki is None:
            return "wiki service not available"
        result = self.wiki.quality_report(search_limit=3)
        embedding = result.get("embedding_coverage", {})
        confidence = result.get("confidence_distribution", {})
        category = result.get("category_coverage", {})
        search = result.get("search_self_test", {})
        return "\n".join(
            [
                "Wiki quality",
                f"pages={result.get('wiki_pages', 0)}",
                f"embedding={_pct(embedding.get('ratio'))} stale={embedding.get('stale', 0)}",
                (
                    "confidence="
                    f"high:{confidence.get('high', 0)} "
                    f"medium:{confidence.get('medium', 0)} "
                    f"low:{confidence.get('low', 0)} "
                    f"unknown:{confidence.get('unknown', 0)}"
                ),
                f"category={_pct(category.get('ratio'))}",
                (
                    f"search_hit={_pct(search.get('hit_rate'))} "
                    f"sample={search.get('sample_size', 0)}"
                ),
            ]
        )

    def _research_response(self, *, status: str | None = None) -> str:
        if self.wiki is None:
            return "wiki service not available"
        candidates = self.wiki.research_candidates(limit=5, status=status)
        if not candidates:
            suffix = f" for status={status}" if status else ""
            return f"Research queue is empty{suffix}."
        lines = ["Research queue"]
        for candidate in candidates:
            queries = candidate.get("source_queries") or []
            first_query = str(queries[0]) if queries else "no source query"
            lines.append(
                f"- [{candidate.get('status', 'unknown')}] {candidate.get('slug', '')} - "
                f"{candidate.get('topic', '')} ({candidate.get('category', 'Research')})"
            )
            lines.append(f"  query: {first_query}")
        return "\n".join(lines)

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


def _pct(value: object) -> str:
    try:
        return f"{float(value or 0.0) * 100:.1f}%"
    except (TypeError, ValueError):
        return "0.0%"
