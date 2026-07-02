from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from claw_v2.bot_commands import CommandContext
from claw_v2.wiki_handler import WikiHandler


def _ctx(text: str) -> CommandContext:
    return CommandContext(user_id="u1", session_id="s1", text=text, stripped=text.strip())


class WikiHandlerTests(unittest.TestCase):
    def test_commands_include_quality_and_research_visibility(self) -> None:
        handler = WikiHandler(wiki=MagicMock())
        command = handler.commands()[0]

        self.assertTrue(command.matches("/wiki quality"))
        self.assertTrue(command.matches("/wiki research"))

    def test_quality_command_returns_concise_metrics(self) -> None:
        wiki = MagicMock()
        wiki.quality_report.return_value = {
            "wiki_pages": 10,
            "embedding_coverage": {"ratio": 0.9, "stale": 1},
            "confidence_distribution": {"high": 2, "medium": 5, "low": 3, "unknown": 0},
            "category_coverage": {"ratio": 0.7},
            "search_self_test": {"hit_rate": 0.6, "sample_size": 10},
        }
        handler = WikiHandler(wiki=wiki)

        reply = handler.handle_command(_ctx("/wiki quality"))

        self.assertIn("Wiki quality", reply)
        self.assertIn("pages=10", reply)
        self.assertIn("embedding=90.0%", reply)
        self.assertIn("search_hit=60.0%", reply)

    def test_research_command_lists_candidate_queue(self) -> None:
        wiki = MagicMock()
        wiki.research_candidates.return_value = [
            {
                "slug": "computer-use-runbook",
                "topic": "Computer Use Runbook",
                "status": "new",
                "category": "Operaciones Dr. Strange",
                "source_queries": ["OpenAI computer use best practices"],
            }
        ]
        handler = WikiHandler(wiki=wiki)

        reply = handler.handle_command(_ctx("/wiki research"))

        self.assertIn("Research queue", reply)
        self.assertIn("computer-use-runbook", reply)
        self.assertIn("OpenAI computer use best practices", reply)

    def test_research_command_passes_status_filter(self) -> None:
        wiki = MagicMock()
        wiki.research_candidates.return_value = [
            {
                "slug": "blocked-runbook",
                "topic": "Blocked Runbook",
                "status": "blocked",
                "category": "Research",
                "source_queries": ["blocked query"],
            }
        ]
        handler = WikiHandler(wiki=wiki)

        reply = handler.handle_command(_ctx("/wiki research blocked"))

        wiki.research_candidates.assert_called_once_with(limit=5, status="blocked")
        self.assertIn("Research queue", reply)
        self.assertIn("blocked-runbook", reply)
        self.assertIn("blocked query", reply)

    def test_research_command_handles_empty_queue(self) -> None:
        wiki = MagicMock()
        wiki.research_candidates.return_value = []
        handler = WikiHandler(wiki=wiki)

        reply = handler.handle_command(_ctx("/wiki research"))

        self.assertIn("Research queue is empty", reply)


if __name__ == "__main__":
    unittest.main()
