from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.wiki import WikiService, _slugify


def _make_wiki(**overrides):
    """Create a WikiService with a temp directory and mock router."""
    tmp = tempfile.mkdtemp()
    router = MagicMock()
    defaults = dict(router=router, wiki_root=Path(tmp), lane="research")
    defaults.update(overrides)
    svc = WikiService(**defaults)
    return svc, router, Path(tmp)


def _write_page(wiki_dir: Path, slug: str, title: str, body: str, **fm_extra) -> Path:
    """Helper to write a wiki page with frontmatter."""
    extra = "\n".join(f"{k}: {v}" for k, v in fm_extra.items())
    content = (
        f"---\ntitle: {title}\ntags: [test]\ncategory: Test\n"
        f"sources: []\ncreated: 2026-01-01T00:00:00Z\nupdated: 2026-01-01T00:00:00Z\n"
        f"{extra}\n---\n\n# {title}\n\n{body}"
    )
    path = wiki_dir / f"{slug}.md"
    path.write_text(content, encoding="utf-8")
    return path


class SlugifyTests(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual(_slugify("Hello World"), "hello-world")

    def test_special_chars(self) -> None:
        slug = _slugify("AI & Tools: A Guide!")
        self.assertNotIn("&", slug)
        self.assertNotIn("!", slug)

    def test_truncates_long(self) -> None:
        slug = _slugify("x" * 200)
        self.assertLessEqual(len(slug), 80)


class LintTests(unittest.TestCase):
    def test_empty_wiki(self) -> None:
        svc, _, _ = _make_wiki()
        result = svc.lint()
        self.assertEqual(result["issues"], 0)

    def test_detects_orphans(self) -> None:
        svc, _, tmp = _make_wiki()
        _write_page(svc.wiki_dir, "page-a", "Page A", "Some content.")
        _write_page(svc.wiki_dir, "page-b", "Page B", "Links to [[page-a]].")
        result = svc.lint()
        # page-b has no inbound links → orphan
        self.assertIn("page-b", result["orphans"])

    def test_detects_missing_links(self) -> None:
        svc, _, tmp = _make_wiki()
        _write_page(svc.wiki_dir, "page-a", "Page A", "See [[nonexistent-page]].")
        result = svc.lint()
        self.assertIn("nonexistent-page", result["missing"])

    def test_no_issues_with_bidirectional_links(self) -> None:
        svc, _, tmp = _make_wiki()
        _write_page(svc.wiki_dir, "page-a", "Page A", "See [[page-b]].")
        _write_page(svc.wiki_dir, "page-b", "Page B", "See [[page-a]].")
        result = svc.lint()
        self.assertEqual(result["orphans"], [])
        self.assertEqual(result["missing"], [])


class DeepLintTests(unittest.TestCase):
    def test_empty_wiki(self) -> None:
        svc, _, _ = _make_wiki()
        result = svc.deep_lint()
        self.assertEqual(result["issues"], 0)
        self.assertEqual(result["contradictions"], [])

    def test_calls_llm_and_parses_response(self) -> None:
        svc, router, tmp = _make_wiki()
        _write_page(svc.wiki_dir, "ai-tools", "AI Tools", "GPT-4 is the best model.")
        _write_page(svc.wiki_dir, "models", "Models", "Claude is the best model. See [[ai-tools]].")

        llm_response = json.dumps(
            {
                "contradictions": [
                    {
                        "pages": ["ai-tools", "models"],
                        "description": "Both claim different models are 'the best'",
                    }
                ],
                "stale": [],
                "gaps": [
                    {
                        "topic": "benchmarks",
                        "mentioned_in": ["models"],
                        "description": "No benchmark page",
                    }
                ],
                "suggestions": [
                    {"action": "update", "target": "ai-tools", "reason": "Clarify ranking criteria"}
                ],
            }
        )
        router.ask.return_value = MagicMock(content=llm_response)

        result = svc.deep_lint()

        self.assertEqual(len(result["contradictions"]), 1)
        self.assertEqual(len(result["gaps"]), 1)
        self.assertEqual(len(result["suggestions"]), 1)
        self.assertGreater(result["issues"], 0)
        router.ask.assert_called_once()

    def test_handles_llm_failure(self) -> None:
        svc, router, tmp = _make_wiki()
        _write_page(svc.wiki_dir, "page-a", "Page A", "Content.")
        router.ask.side_effect = RuntimeError("API error")

        result = svc.deep_lint()

        self.assertEqual(result["contradictions"], [])
        self.assertEqual(result["stale"], [])
        self.assertEqual(result["gaps"], [])

    def test_includes_structural_issues(self) -> None:
        svc, router, tmp = _make_wiki()
        _write_page(svc.wiki_dir, "orphan", "Orphan", "No links to me.")
        router.ask.return_value = MagicMock(
            content=json.dumps(
                {
                    "contradictions": [],
                    "stale": [],
                    "gaps": [],
                    "suggestions": [],
                }
            )
        )

        result = svc.deep_lint()

        self.assertIn("orphan", result["orphans"])
        self.assertGreater(result["issues"], 0)

    def test_appends_to_log(self) -> None:
        svc, router, tmp = _make_wiki()
        _write_page(svc.wiki_dir, "page-a", "Page A", "Content.")
        router.ask.return_value = MagicMock(
            content=json.dumps(
                {
                    "contradictions": [],
                    "stale": [],
                    "gaps": [],
                    "suggestions": [],
                }
            )
        )

        svc.deep_lint()

        log = svc.log_path.read_text(encoding="utf-8")
        self.assertIn("deep_lint", log)

    def test_auto_fix_creates_stub_without_auto_fill_query(self) -> None:
        svc, router, tmp = _make_wiki()
        _write_page(svc.wiki_dir, "models", "Models", "Mentions missing [[benchmarks]].")
        router.ask.return_value = MagicMock(
            content=json.dumps(
                {
                    "contradictions": [],
                    "stale": [],
                    "gaps": [
                        {
                            "topic": "benchmarks",
                            "mentioned_in": ["models"],
                            "description": "No benchmark page",
                        }
                    ],
                    "suggestions": [],
                }
            )
        )

        result = svc.deep_lint(auto_fix=True)

        self.assertIn("stub:benchmarks", result["auto_fixed"])
        stub = (svc.wiki_dir / "benchmarks.md").read_text(encoding="utf-8")
        self.assertIn("Requires raw source evidence", stub)
        self.assertNotIn("Síntesis automática", stub)
        router.ask.assert_called_once()


class SearchTests(unittest.TestCase):
    def test_search_returns_results(self) -> None:
        svc, _, tmp = _make_wiki()
        _write_page(svc.wiki_dir, "ai-tools", "AI Tools Overview", "LLMs are powerful.")
        results = svc.search("AI tools")
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0]["slug"], "ai-tools")
        self.assertIn("score", results[0])
        self.assertIn("keyword_score", results[0])

    def test_search_empty_wiki(self) -> None:
        svc, _, _ = _make_wiki()
        results = svc.search("anything")
        self.assertEqual(results, [])

    def test_search_exact_keyword_can_win(self) -> None:
        svc, _, tmp = _make_wiki()
        _write_page(
            svc.wiki_dir, "general-errors", "General Errors", "Generic troubleshooting guide."
        )
        _write_page(
            svc.wiki_dir, "error-404", "HTTP Error", "The exact code Error 404 means not found."
        )

        results = svc.search("Error 404")

        self.assertGreater(len(results), 0)
        self.assertEqual(results[0]["slug"], "error-404")
        self.assertGreater(results[0]["keyword_score"], 0)


class StatsTests(unittest.TestCase):
    def test_counts_files(self) -> None:
        svc, _, tmp = _make_wiki()
        _write_page(svc.wiki_dir, "page-a", "A", "content")
        (svc.raw_dir / "source.md").write_text("raw", encoding="utf-8")
        stats = svc.stats()
        self.assertEqual(stats["wiki_pages"], 1)
        self.assertEqual(stats["raw_sources"], 1)

    def test_quality_report_measures_retrieval_and_coverage_without_mutating_embeddings(
        self,
    ) -> None:
        svc, _, tmp = _make_wiki()
        _write_page(
            svc.wiki_dir,
            "ai-tools",
            "AI Tools",
            "LLM tools and embeddings.",
            category="AI & Herramientas",
            confidence="0.8",
        )
        _write_page(
            svc.wiki_dir,
            "ops-runbook",
            "Operations Runbook",
            "Claw operational procedures.",
            category="Operaciones Dr. Strange",
            confidence="0.55",
        )
        _write_page(
            svc.wiki_dir,
            "insurance",
            "Insurance Notes",
            "Coverage details.",
            category="Seguros",
            confidence="0.2",
        )
        _write_page(
            svc.wiki_dir,
            "unknown-topic",
            "Unknown Topic",
            "A page without calibrated confidence.",
            category="Unknown Bucket",
        )
        svc._embeddings["ops-runbook"] = [0.1] * 128
        svc._embeddings["stale-page"] = [0.2] * 128

        report = svc.quality_report(search_limit=1)

        self.assertEqual(report["wiki_pages"], 4)
        self.assertEqual(report["embedding_coverage"]["indexed"], 1)
        self.assertEqual(report["embedding_coverage"]["total"], 4)
        self.assertEqual(report["embedding_coverage"]["ratio"], 0.25)
        self.assertEqual(report["embedding_coverage"]["stale"], 1)
        self.assertEqual(
            report["confidence_distribution"],
            {"high": 1, "medium": 1, "low": 1, "unknown": 1},
        )
        self.assertEqual(report["category_coverage"]["covered"], 3)
        self.assertEqual(report["category_coverage"]["unmapped"], 1)
        self.assertEqual(report["category_coverage"]["distribution"]["AI & Herramientas"], 1)
        self.assertEqual(report["category_coverage"]["distribution"]["Operaciones Dr. Strange"], 1)
        self.assertEqual(report["category_coverage"]["distribution"]["Seguros"], 1)
        self.assertEqual(report["search_self_test"]["sample_size"], 4)
        self.assertEqual(report["search_self_test"]["hit_rate"], 1.0)
        self.assertEqual(set(svc._embeddings), {"ops-runbook", "stale-page"})


class GraphTests(unittest.TestCase):
    def test_load_empty_graph(self) -> None:
        svc, _, _ = _make_wiki()
        self.assertEqual(svc._graph, {})

    def test_update_graph_from_analysis(self) -> None:
        svc, _, _ = _make_wiki()
        analysis = {
            "entities": [{"name": "LLM", "type": "concept"}],
            "relations": [
                {"source": "LLM", "target": "Transformer", "type": "uses", "weight": 0.9}
            ],
        }
        svc._update_graph_from_analysis("llm-overview", analysis)
        self.assertIn("llm", svc._graph)
        self.assertEqual(svc._graph["llm"][0]["target"], "transformer")
        # Reverse edge
        self.assertIn("transformer", svc._graph)

    def test_save_and_reload_graph(self) -> None:
        svc, router, tmp = _make_wiki()
        svc._graph = {
            "a": [{"target": "b", "type": "relates_to", "weight": 0.5, "source_page": "x"}]
        }
        svc._save_graph()
        self.assertTrue(svc._graph_path.exists())
        # Reload
        svc2 = WikiService(router=router, wiki_root=Path(tmp), lane="research")
        self.assertEqual(svc2._graph["a"][0]["target"], "b")

    def test_graph_neighbors(self) -> None:
        svc, _, _ = _make_wiki()
        svc._graph = {
            "a": [{"target": "b", "type": "r", "weight": 1, "source_page": "x"}],
            "b": [{"target": "c", "type": "r", "weight": 1, "source_page": "x"}],
        }
        neighbors = svc._graph_neighbors("a", depth=1)
        self.assertIn("b", neighbors)
        self.assertNotIn("c", neighbors)
        neighbors2 = svc._graph_neighbors("a", depth=2)
        self.assertIn("c", neighbors2)

    def test_no_duplicate_edges(self) -> None:
        svc, _, _ = _make_wiki()
        analysis = {"relations": [{"source": "A", "target": "B", "type": "uses", "weight": 0.8}]}
        svc._update_graph_from_analysis("page1", analysis)
        svc._update_graph_from_analysis("page1", analysis)
        self.assertEqual(len(svc._graph["a"]), 1)


class DeleteTests(unittest.TestCase):
    def test_cascade_delete(self) -> None:
        svc, _, tmp = _make_wiki()
        # Setup: raw + wiki + embedding + index + graph
        (svc.raw_dir / "test-page.md").write_text("raw source", encoding="utf-8")
        _write_page(svc.wiki_dir, "test-page", "Test Page", "Content")
        svc._embeddings["test-page"] = [0.1] * 128
        svc._save_embeddings()
        svc._graph = {
            "test-page": [
                {"target": "other", "type": "r", "weight": 1, "source_page": "test-page"}
            ],
            "other": [
                {"target": "test-page", "type": "r", "weight": 0.5, "source_page": "test-page"}
            ],
        }
        svc._save_graph()
        svc.index_path.write_text(
            "## Test\n- [[test-page]] — desc\n- [[other]] — keep\n", encoding="utf-8"
        )

        _result = svc.delete("test-page")

        self.assertFalse((svc.raw_dir / "test-page.md").exists())
        self.assertFalse((svc.wiki_dir / "test-page.md").exists())
        self.assertNotIn("test-page", svc._embeddings)
        self.assertNotIn("test-page", svc._graph)
        # Edges referencing test-page removed from other nodes
        self.assertEqual(len(svc._graph.get("other", [])), 0)
        # Index cleaned
        idx = svc.index_path.read_text(encoding="utf-8")
        self.assertNotIn("[[test-page]]", idx)
        self.assertIn("[[other]]", idx)

    def test_delete_nonexistent(self) -> None:
        svc, _, _ = _make_wiki()
        result = svc.delete("no-such-page")
        self.assertEqual(result["removed"], [])


class IngestTests(unittest.TestCase):
    def test_two_step_ingest(self) -> None:
        svc, router, tmp = _make_wiki()
        # Step 1 response (analyze)
        analyze_resp = json.dumps(
            {
                "entities": [{"name": "Test", "type": "concept", "description": "test entity"}],
                "relations": [],
                "key_facts": ["fact1"],
                "category": "Research",
                "tags": ["test"],
                "pages_to_update": [],
                "new_concepts": [],
            }
        )
        # Step 2 response (generate)
        generate_resp = json.dumps(
            {
                "summary_page": {
                    "filename": "test-article.md",
                    "content": "---\ntitle: Test Article\ntags: [test]\n---\n\n# Test Article\n\nContent.",
                },
                "updates": [],
                "new_pages": [],
                "index_entries": [
                    {"category": "Research", "entry": "- [[test-article]] — Test Article"}
                ],
            }
        )
        router.ask.side_effect = [
            MagicMock(content=analyze_resp),
            MagicMock(content=generate_resp),
        ]

        result = svc.ingest("Test Article", "Some test content for wiki ingest.")

        self.assertEqual(result["slug"], "test-article")
        self.assertEqual(result["pages_written"], 1)
        self.assertEqual(router.ask.call_count, 2)
        self.assertTrue((svc.wiki_dir / "test-article.md").exists())
        text = (svc.wiki_dir / "test-article.md").read_text(encoding="utf-8")
        self.assertIn("sources: [test-article]", text)
        self.assertTrue((svc.raw_dir / "test-article.md").exists())


class AutoResearchTests(unittest.TestCase):
    def test_auto_research_returns_candidates_without_writing_pages(self) -> None:
        svc, router, tmp = _make_wiki()
        _write_page(svc.wiki_dir, "existing", "Existing", "Content.")
        router.ask.return_value = MagicMock(
            content=json.dumps(
                [
                    {
                        "topic": "New Topic",
                        "category": "Research",
                        "reason": "Needs source research",
                        "source_queries": ["New Topic primary source"],
                    }
                ]
            )
        )

        result = svc.auto_research(max_topics=1)

        self.assertEqual(result["pages_written"], 0)
        self.assertEqual(result["topics_researched"], 1)
        self.assertFalse((svc.wiki_dir / "new-topic.md").exists())
        self.assertEqual(result["candidates"][0]["slug"], "new-topic")

    def test_auto_research_persists_candidates_as_inspectable_queue(self) -> None:
        svc, router, tmp = _make_wiki()
        _write_page(svc.wiki_dir, "existing", "Existing", "Content.")
        router.ask.return_value = MagicMock(
            content=json.dumps(
                [
                    {
                        "topic": "Computer Use Runbooks",
                        "category": "Operaciones Dr. Strange",
                        "reason": "Recurring browser/computer-use failures need durable guidance.",
                        "source_queries": ["OpenAI computer use best practices"],
                    }
                ]
            )
        )

        result = svc.auto_research(max_topics=1)

        queue_path = tmp / "research_candidates.json"
        self.assertTrue(queue_path.exists())
        queued = json.loads(queue_path.read_text(encoding="utf-8"))
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["slug"], "computer-use-runbooks")
        self.assertEqual(queued[0]["status"], "new")
        self.assertEqual(queued[0]["source"], "auto_research")
        self.assertIn("created_at", queued[0])
        self.assertIn("updated_at", queued[0])
        self.assertEqual(result["candidates"][0]["status"], "new")
        self.assertEqual(svc.research_candidates(limit=1)[0]["slug"], "computer-use-runbooks")

    def test_auto_research_updates_existing_candidate_without_duplicate_queue_rows(self) -> None:
        svc, router, tmp = _make_wiki()
        _write_page(svc.wiki_dir, "existing", "Existing", "Content.")
        router.ask.return_value = MagicMock(
            content=json.dumps(
                [
                    {
                        "topic": "Agent Knowledge Loop",
                        "category": "Research",
                        "reason": "First reason.",
                        "source_queries": ["query one"],
                    }
                ]
            )
        )

        svc.auto_research(max_topics=1)
        router.ask.return_value = MagicMock(
            content=json.dumps(
                [
                    {
                        "topic": "Agent Knowledge Loop",
                        "category": "AI & Herramientas",
                        "reason": "Updated reason.",
                        "source_queries": ["query two"],
                    }
                ]
            )
        )

        svc.auto_research(max_topics=1)

        queued = json.loads((tmp / "research_candidates.json").read_text(encoding="utf-8"))
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["reason"], "Updated reason.")
        self.assertEqual(queued[0]["seen_count"], 2)

    def test_research_candidate_updates_preserve_queue_and_save_atomically(self) -> None:
        svc, router, tmp = _make_wiki()
        svc._save_research_candidates(
            [
                {"slug": "first", "topic": "First", "status": "new"},
                {"slug": "second", "topic": "Second", "status": "new"},
            ]
        )

        with patch("os.replace", wraps=__import__("os").replace) as replace:
            svc._update_research_candidate("first", {"status": "blocked"})

        queued = json.loads((tmp / "research_candidates.json").read_text(encoding="utf-8"))
        self.assertEqual(len(queued), 2)
        self.assertEqual(queued[0]["status"], "blocked")
        self.assertEqual(queued[1]["status"], "new")
        replace.assert_called()

    def test_auto_research_tolerates_null_source_queries(self) -> None:
        svc, router, tmp = _make_wiki()
        _write_page(svc.wiki_dir, "existing", "Existing", "Content.")
        router.ask.return_value = MagicMock(
            content=json.dumps(
                [
                    {
                        "topic": "Null Query Topic",
                        "category": "Research",
                        "reason": "LLM returned null source queries.",
                        "source_queries": None,
                    }
                ]
            )
        )

        result = svc.auto_research(max_topics=1)

        self.assertEqual(result["topics_researched"], 1)
        self.assertEqual(result["candidates"][0]["source_queries"], [])

    def test_auto_research_worker_writes_raw_evidence_without_wiki_page(self) -> None:
        svc, router, tmp = _make_wiki()
        _write_page(svc.wiki_dir, "existing", "Existing", "Content.")
        router.ask.side_effect = [
            MagicMock(
                content=json.dumps(
                    [
                        {
                            "topic": "Computer Use Runbooks",
                            "category": "Operaciones Dr. Strange",
                            "reason": "Recurring automation failures need durable guidance.",
                            "source_queries": ["computer use agents best practices"],
                        }
                    ]
                )
            ),
            MagicMock(
                content=json.dumps(
                    {
                        "summary": "Computer-use agents need bounded tools and observable outcomes.",
                        "confidence": "medium",
                        "sources": [
                            {
                                "title": "Computer use guide",
                                "url": "https://example.com/computer-use",
                                "source_kind": "primary",
                                "published_at": "2026-06-01",
                                "evidence": "Agents should run with scoped tools and verification.",
                            }
                        ],
                    }
                )
            ),
        ]

        result = svc.auto_research(max_topics=1, research_limit=1)

        self.assertEqual(result["topics_researched"], 1)
        self.assertEqual(result["candidates_researched"], 1)
        self.assertEqual(result["raw_sources_written"], 1)
        self.assertEqual(result["candidates_blocked"], 0)
        self.assertFalse((svc.wiki_dir / "computer-use-runbooks.md").exists())
        raw_path = svc.raw_dir / "research-computer-use-runbooks.md"
        self.assertTrue(raw_path.exists())
        raw_text = raw_path.read_text(encoding="utf-8")
        self.assertIn("Computer-use agents need bounded tools", raw_text)
        self.assertIn("https://example.com/computer-use", raw_text)
        candidate = svc.research_candidates(limit=1)[0]
        self.assertEqual(candidate["status"], "researched")
        self.assertEqual(candidate["raw_source_slug"], "research-computer-use-runbooks")
        self.assertEqual(candidate["sources_count"], 1)
        self.assertEqual(router.ask.call_args_list[1].kwargs["lane"], "worker")
        self.assertIn("WebSearch", router.ask.call_args_list[1].kwargs["allowed_tools"])

    def test_auto_research_worker_blocks_candidate_without_sources(self) -> None:
        svc, router, tmp = _make_wiki()
        _write_page(svc.wiki_dir, "existing", "Existing", "Content.")
        router.ask.side_effect = [
            MagicMock(
                content=json.dumps(
                    [
                        {
                            "topic": "Unverifiable Topic",
                            "category": "Research",
                            "reason": "Needs evidence.",
                            "source_queries": ["unverifiable topic"],
                        }
                    ]
                )
            ),
            MagicMock(
                content=json.dumps(
                    {
                        "summary": "",
                        "confidence": "low",
                        "sources": [],
                        "blocked_reason": "No primary sources found.",
                    }
                )
            ),
        ]

        result = svc.auto_research(max_topics=1, research_limit=1)

        self.assertEqual(result["candidates_researched"], 0)
        self.assertEqual(result["raw_sources_written"], 0)
        self.assertEqual(result["candidates_blocked"], 1)
        candidate = svc.research_candidates(limit=1)[0]
        self.assertEqual(candidate["status"], "blocked")
        self.assertEqual(candidate["blocked_reason"], "No primary sources found.")
        self.assertFalse((svc.raw_dir / "research-unverifiable-topic.md").exists())

    def test_compile_researched_candidate_writes_wiki_page_after_quality_gate(self) -> None:
        svc, router, tmp = _make_wiki()
        raw_slug = "research-computer-use-runbooks"
        (svc.raw_dir / f"{raw_slug}.md").write_text(
            "---\ntitle: Computer Use Runbooks\ntype: auto-research\n---\n\n"
            "# Computer Use Runbooks\n\nRaw evidence with sources.",
            encoding="utf-8",
        )
        svc._save_research_candidates(
            [
                {
                    "slug": "computer-use-runbooks",
                    "topic": "Computer Use Runbooks",
                    "category": "Operaciones Dr. Strange",
                    "status": "researched",
                    "raw_source_slug": raw_slug,
                    "sources_count": 1,
                }
            ]
        )
        router.ask.return_value = MagicMock(
            content=json.dumps(
                {
                    "summary_page": {
                        "filename": "computer-use-runbooks.md",
                        "content": (
                            "---\n"
                            "title: Computer Use Runbooks\n"
                            "tags: [computer-use, runbook]\n"
                            "category: Operaciones Dr. Strange\n"
                            f"sources: [{raw_slug}]\n"
                            "created: 2026-06-29T00:00:00+00:00\n"
                            "updated: 2026-06-29T00:00:00+00:00\n"
                            "---\n\n"
                            "# Computer Use Runbooks\n\n"
                            "Operational guidance compiled from raw evidence."
                        ),
                    },
                    "index_entry": "- [[computer-use-runbooks]] - Computer Use Runbooks",
                    "category": "Operaciones Dr. Strange",
                }
            )
        )

        result = svc.compile_researched_candidates(max_candidates=1)

        self.assertEqual(result["candidates_compiled"], 1)
        self.assertEqual(result["compile_blocked"], 0)
        self.assertEqual(result["pages_written"], 1)
        page = svc.wiki_dir / "computer-use-runbooks.md"
        self.assertTrue(page.exists())
        text = page.read_text(encoding="utf-8")
        self.assertIn(f"sources: [{raw_slug}]", text)
        self.assertIn("confidence:", text)
        candidate = svc.research_candidates(limit=1)[0]
        self.assertEqual(candidate["status"], "compiled")
        self.assertEqual(candidate["compiled_page_slug"], "computer-use-runbooks")
        self.assertIn("[[computer-use-runbooks]]", svc.index_path.read_text(encoding="utf-8"))

    def test_auto_research_compile_limit_promotes_existing_researched_candidate(self) -> None:
        svc, router, tmp = _make_wiki()
        _write_page(svc.wiki_dir, "existing", "Existing", "Content.")
        raw_slug = "research-existing-candidate"
        (svc.raw_dir / f"{raw_slug}.md").write_text(
            "---\ntitle: Existing Candidate\ntype: auto-research\n---\n\nRaw evidence.",
            encoding="utf-8",
        )
        svc._save_research_candidates(
            [
                {
                    "slug": "existing-candidate",
                    "topic": "Existing Candidate",
                    "category": "Research",
                    "status": "researched",
                    "raw_source_slug": raw_slug,
                }
            ]
        )
        router.ask.side_effect = [
            MagicMock(content="[]"),
            MagicMock(
                content=json.dumps(
                    {
                        "summary_page": {
                            "filename": "existing-candidate.md",
                            "content": (
                                "---\n"
                                "title: Existing Candidate\n"
                                "tags: [research]\n"
                                "category: Research\n"
                                f"sources: [{raw_slug}]\n"
                                "created: 2026-06-29T00:00:00+00:00\n"
                                "updated: 2026-06-29T00:00:00+00:00\n"
                                "---\n\n"
                                "# Existing Candidate\n\nCompiled from raw evidence."
                            ),
                        },
                        "index_entry": "- [[existing-candidate]] - Existing Candidate",
                        "category": "Research",
                    }
                )
            ),
        ]

        result = svc.auto_research(max_topics=1, compile_limit=1)

        self.assertEqual(result["topics_researched"], 0)
        self.assertEqual(result["candidates_compiled"], 1)
        self.assertEqual(result["pages_written"], 1)
        self.assertTrue((svc.wiki_dir / "existing-candidate.md").exists())

    def test_compile_researched_candidate_blocks_when_raw_source_missing(self) -> None:
        svc, router, tmp = _make_wiki()
        svc._save_research_candidates(
            [
                {
                    "slug": "missing-raw",
                    "topic": "Missing Raw",
                    "status": "researched",
                    "raw_source_slug": "research-missing-raw",
                }
            ]
        )

        result = svc.compile_researched_candidates(max_candidates=1)

        self.assertEqual(result["candidates_compiled"], 0)
        self.assertEqual(result["compile_blocked"], 1)
        candidate = svc.research_candidates(limit=1)[0]
        self.assertEqual(candidate["status"], "compile_blocked")
        self.assertEqual(candidate["compile_blocked_reason"], "raw_source_missing")
        router.ask.assert_not_called()

    def test_compile_researched_candidate_blocks_when_quality_gate_fails(self) -> None:
        svc, router, tmp = _make_wiki()
        raw_slug = "research-low-quality"
        (svc.raw_dir / f"{raw_slug}.md").write_text("raw evidence", encoding="utf-8")
        svc._save_research_candidates(
            [
                {
                    "slug": "low-quality",
                    "topic": "Low Quality",
                    "status": "researched",
                    "raw_source_slug": raw_slug,
                }
            ]
        )
        router.ask.return_value = MagicMock(
            content=json.dumps(
                {
                    "summary_page": {
                        "filename": "low-quality.md",
                        "content": "# Low Quality\n\nMissing frontmatter and sources.",
                    }
                }
            )
        )

        result = svc.compile_researched_candidates(max_candidates=1)

        self.assertEqual(result["candidates_compiled"], 0)
        self.assertEqual(result["compile_blocked"], 1)
        self.assertFalse((svc.wiki_dir / "low-quality.md").exists())
        candidate = svc.research_candidates(limit=1)[0]
        self.assertEqual(candidate["status"], "compile_blocked")
        self.assertEqual(candidate["compile_blocked_reason"], "quality_gate_failed")


class EvidenceTests(unittest.TestCase):
    def test_raw_source_detection(self) -> None:
        svc, _, tmp = _make_wiki()
        (svc.raw_dir / "source-page.md").write_text("raw", encoding="utf-8")

        self.assertTrue(svc._has_raw_evidence(["source-page"]))
        self.assertFalse(svc._has_raw_evidence(["missing-source"]))


class TokenBudgetQueryTests(unittest.TestCase):
    def test_query_respects_token_budget(self) -> None:
        svc, router, tmp = _make_wiki()
        # Create pages with known sizes
        _write_page(svc.wiki_dir, "big-page", "Big Page", "x" * 5000)
        _write_page(svc.wiki_dir, "small-page", "Small Page", "y" * 100)

        router.ask.return_value = MagicMock(content="Answer based on wiki.")

        # Small budget: should truncate content
        svc.query("test question", token_budget=500)
        call_args = router.ask.call_args
        prompt = call_args[0][0]
        # Context should be well under 5000 chars of big-page
        self.assertLess(len(prompt), 8000)


class AutoScrapeTests(unittest.TestCase):
    def test_auto_scrape_ingests_through_raw_pipeline(self) -> None:
        svc, router, tmp = _make_wiki()
        svc.WATCH_SOURCES = [("Test Source", "https://example.com/source")]
        scrape_extract = json.dumps(
            [
                {
                    "title": "Scraped Topic",
                    "content": "Specific sourced fact with enough detail for ingestion.",
                    "category": "Research",
                }
            ]
        )
        analyze_resp = json.dumps(
            {
                "entities": [],
                "relations": [],
                "key_facts": ["fact"],
                "category": "Research",
                "tags": ["test"],
                "pages_to_update": [],
                "new_concepts": [],
            }
        )
        generate_resp = json.dumps(
            {
                "summary_page": {
                    "filename": "scraped-topic.md",
                    "content": "---\ntitle: Scraped Topic\n---\n\n# Scraped Topic\n\nContent.",
                },
                "updates": [],
                "new_pages": [],
                "index_entries": [],
            }
        )
        router.ask.side_effect = [
            MagicMock(content=scrape_extract),
            MagicMock(content=analyze_resp),
            MagicMock(content=generate_resp),
        ]
        proc = MagicMock(returncode=0, stdout="source page content", stderr="")

        with patch("subprocess.run", return_value=proc):
            result = svc.auto_scrape_sources()

        self.assertGreaterEqual(result["pages_ingested"], 1)
        self.assertTrue((svc.raw_dir / "scraped-topic.md").exists())
        text = (svc.wiki_dir / "scraped-topic.md").read_text(encoding="utf-8")
        self.assertIn("sources: [scraped-topic]", text)

    def test_auto_scrape_pauses_when_firecrawl_credits_are_exhausted(self) -> None:
        observe = MagicMock()
        svc, router, tmp = _make_wiki(observe=observe)
        svc.WATCH_SOURCES = [("Test Source", "https://example.com/source")]
        proc = MagicMock(returncode=1, stdout="", stderr="Payment required: insufficient credits")

        with patch("subprocess.run", return_value=proc):
            result = svc.auto_scrape_sources()

        self.assertEqual(result["sources_scraped"], 0)
        self.assertEqual(result["sources_skipped"], 1)
        self.assertGreater(svc._firecrawl_paused_until, 0)
        observe.emit.assert_any_call(
            "firecrawl_paused",
            payload={
                "reason": "insufficient_credits",
                "paused_seconds": 86400,
                "paused_until": svc._firecrawl_paused_until,
            },
        )
        router.ask.assert_not_called()

    def test_auto_scrape_skips_while_firecrawl_is_paused(self) -> None:
        observe = MagicMock()
        svc, _, _ = _make_wiki(observe=observe)
        svc._firecrawl_paused_until = time.time() + 60
        svc._firecrawl_pause_reason = "insufficient_credits"

        result = svc.auto_scrape_sources()

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "firecrawl_paused")

    def test_auto_scrape_reports_item_skip_reasons(self) -> None:
        svc, router, tmp = _make_wiki()
        svc.WATCH_SOURCES = [("Test Source", "https://example.com/source")]
        router.ask.return_value = MagicMock(
            content=json.dumps(
                [
                    {
                        "title": "",
                        "content": "Specific sourced fact with enough detail for ingestion.",
                        "category": "Research",
                    },
                    {
                        "title": "Too Short",
                        "content": "tiny",
                        "category": "Research",
                    },
                ]
            )
        )
        proc = MagicMock(returncode=0, stdout="source page content", stderr="")

        with patch("subprocess.run", return_value=proc):
            result = svc.auto_scrape_sources()

        self.assertEqual(result["pages_ingested"], 0)
        self.assertEqual(result["source_results"][0]["items_extracted"], 2)
        self.assertEqual(result["source_results"][0]["items_skipped"], 2)
        self.assertEqual(result["source_results"][0]["skip_reasons"]["missing_title"], 1)
        self.assertEqual(result["source_results"][0]["skip_reasons"]["body_too_short"], 1)
        reasons = [item["reason"] for item in result["item_results"]]
        self.assertEqual(reasons, ["missing_title", "body_too_short"])

    def test_auto_scrape_skips_non_mapping_llm_items(self) -> None:
        svc, router, tmp = _make_wiki()
        svc.WATCH_SOURCES = [("Test Source", "https://example.com/source")]
        router.ask.return_value = MagicMock(
            content=json.dumps(
                [
                    "raw string item",
                    {
                        "title": "Valid Topic",
                        "content": "Specific sourced fact with enough detail for ingestion.",
                        "category": "Research",
                    },
                ]
            )
        )
        proc = MagicMock(returncode=0, stdout="source page content", stderr="")

        with patch("subprocess.run", return_value=proc), patch.object(
            svc, "ingest", return_value={"pages_written": 0, "skipped": True}
        ):
            result = svc.auto_scrape_sources()

        self.assertEqual(result["source_results"][0]["items_extracted"], 2)
        self.assertEqual(result["source_results"][0]["skip_reasons"]["invalid_item"], 1)
        self.assertEqual(result["item_results"][0]["reason"], "invalid_item")


if __name__ == "__main__":
    unittest.main()
