"""LLM Wiki — persistent, compounding knowledge base following the Karpathy pattern.

Layers:
  raw/   — immutable source documents
  wiki/  — LLM-generated interlinked markdown pages
  index.md — navigable catalog by category
  log.md — append-only operation timeline
"""
from __future__ import annotations

import json
import logging
import math
import re
import textwrap
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claw_v2.llm import LLMRouter
    from claw_v2.observe import ObserveStream

logger = logging.getLogger(__name__)

_DEFAULT_WIKI_ROOT = Path.home() / ".claw" / "wiki"
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_FRONTMATTER_RE = re.compile(r"^---\n(.+?)\n---", re.DOTALL)
_TOKEN_RE = re.compile(r"[\w][\w-]*", re.IGNORECASE)
_MIN_CONTEXT_CHARS = 200
_DEFAULT_CHUNK_CHARS = 2400
_CHUNK_OVERLAP_CHARS = 160

VALID_CATEGORIES = [
    "AI & Herramientas",
    "Clientes & Proyectos",
    "Operaciones Claw",
    "Seguros",
    "Diseno & Web",
    "Personas",
    "Research",
]

_FIRECRAWL_CREDIT_PATTERNS = (
    "insufficient credits",
    "not enough credits",
    "credit balance",
    "payment required",
)
_FIRECRAWL_RATE_LIMIT_PATTERNS = (
    "rate limit",
    "too many requests",
    "429",
)


def classify_firecrawl_failure(text: str) -> str | None:
    normalized = (text or "").lower()
    if any(pattern in normalized for pattern in _FIRECRAWL_CREDIT_PATTERNS):
        return "insufficient_credits"
    if any(pattern in normalized for pattern in _FIRECRAWL_RATE_LIMIT_PATTERNS):
        return "rate_limited"
    return None


def _tokenize(text: str) -> list[str]:
    return [tok.lower() for tok in _TOKEN_RE.findall(text)]

# ---------- Embedding helpers (shared with memory.py pattern) ----------

_ST_MODEL = None
_ST_LOCK = threading.Lock()


def _get_st_model():
    global _ST_MODEL
    if _ST_MODEL is None:
        with _ST_LOCK:
            if _ST_MODEL is None:
                try:
                    from sentence_transformers import SentenceTransformer
                    _ST_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
                except Exception:
                    _ST_MODEL = False
    return _ST_MODEL if _ST_MODEL is not False else None


def _embed(text: str) -> list[float]:
    model = _get_st_model()
    if model is not None:
        return model.encode(text, normalize_embeddings=True).tolist()
    # Fallback: bag-of-chars
    dim = 128
    vec = [0.0] * dim
    for i, ch in enumerate(text.lower()):
        vec[ord(ch) % dim] += 1.0 / (1 + i * 0.01)
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class WikiService:
    """Manages the persistent LLM wiki."""

    def __init__(
        self,
        *,
        router: LLMRouter,
        wiki_root: Path | None = None,
        lane: str = "research",
        observe: ObserveStream | None = None,
    ) -> None:
        self.router = router
        self.observe = observe
        self.root = wiki_root or _DEFAULT_WIKI_ROOT
        self.raw_dir = self.root / "raw"
        self.wiki_dir = self.root / "wiki"
        self.index_path = self.root / "index.md"
        self.log_path = self.root / "log.md"
        self.schema_path = self.root / "schema.md"
        self._embeddings_path = self.root / "embeddings.json"
        self._graph_path = self.root / "graph.json"
        self.lane = lane
        # Ensure dirs exist
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        # In-memory indices
        self._lock = threading.Lock()
        self._embeddings: dict[str, list[float]] = self._load_embeddings()
        self._graph: dict[str, list[dict]] = self._load_graph()
        self._firecrawl_paused_until = 0.0
        self._firecrawl_pause_reason = ""

    # ------------------------------------------------------------------
    # Ingest (two-step chain-of-thought)
    # ------------------------------------------------------------------

    def ingest(self, title: str, content: str, *, source_type: str = "article") -> dict:
        """Two-step ingest: (1) analyze → entities & relations, (2) generate wiki pages."""
        slug = _slugify(title)
        now = _now_iso()

        raw_path = self.raw_dir / f"{slug}.md"
        if raw_path.exists() and self._is_deprecated(raw_path):
            return {"slug": slug, "raw_path": str(raw_path), "pages_written": 0, "skipped": True}

        # Dedup: skip if content is too similar to an existing page
        dup = self._find_duplicate(content)
        if dup:
            logger.info("Skipping ingest of '%s': duplicate of '%s'", title, dup)
            return {"slug": slug, "raw_path": "", "pages_written": 0, "skipped": True, "duplicate_of": dup}

        raw_path.write_text(
            f"---\ntitle: {title}\ntype: {source_type}\ningested: {now}\n---\n\n{content}",
            encoding="utf-8",
        )

        index_text = self.index_path.read_text(encoding="utf-8") if self.index_path.exists() else ""
        existing_summaries = "\n".join(
            f"- [[{p.stem}]]: {self._extract_title(p)}" for p in self._list_wiki_pages()[:30]
        )
        schema_text = ""
        if self.schema_path.exists():
            try:
                schema_text = self.schema_path.read_text(encoding="utf-8")[:3000]
            except Exception:
                pass

        # Step 1: Analyze
        analysis = self._ingest_analyze(title, content, source_type, existing_summaries, schema_text)

        # Step 2: Generate
        result = self._ingest_generate(slug, title, content, analysis, existing_summaries, index_text, schema_text, now)

        pages_written = 0
        summary = result.get("summary_page", {})
        if summary.get("content"):
            target = self.wiki_dir / summary["filename"]
            target.write_text(summary["content"], encoding="utf-8")
            self._ensure_raw_source(target, slug)
            summary_content = target.read_text(encoding="utf-8")
            self._index_page_embedding(Path(summary["filename"]).stem, summary_content)
            pages_written += 1

        for update in result.get("updates", []):
            if update.get("filename") and update.get("content"):
                target = self.wiki_dir / update["filename"]
                if target.exists():
                    target.write_text(update["content"], encoding="utf-8")
                    self._ensure_raw_source(target, slug)
                    self._index_page_embedding(target.stem, target.read_text(encoding="utf-8"))
                    pages_written += 1

        for new_page in result.get("new_pages", []):
            if new_page.get("filename") and new_page.get("content"):
                target = self.wiki_dir / new_page["filename"]
                if not target.exists():
                    target.write_text(new_page["content"], encoding="utf-8")
                    self._ensure_raw_source(target, slug)
                    self._index_page_embedding(target.stem, target.read_text(encoding="utf-8"))
                    pages_written += 1

        for entry in result.get("index_entries", []):
            self._update_index(entry.get("category", "Research"), entry.get("entry", ""))

        self._update_graph_from_analysis(slug, analysis)
        self._save_embeddings()
        self._save_graph()

        # Confidence scoring: auto-calculate and store in frontmatter
        confidence = self._compute_confidence(slug)
        summary_path = self.wiki_dir / f"{slug}.md"
        if summary_path.exists():
            self._set_frontmatter_field(summary_path, "confidence", str(confidence))

        # Supersession: mark pages with contradicted claims
        for sup in analysis.get("supersedes", []):
            target_slug = _slugify(sup.get("page", ""))
            target_path = self.wiki_dir / f"{target_slug}.md"
            if target_path.exists():
                self._set_frontmatter_field(target_path, "superseded_by", f"[[{slug}]]")
                logger.info("Marked %s as superseded by %s", target_slug, slug)

        self._append_log("ingest", title, pages_written)

        return {
            "slug": slug, "raw_path": str(raw_path), "pages_written": pages_written,
            "updates": len(result.get("updates", [])), "new_pages": len(result.get("new_pages", [])),
            "entities": len(analysis.get("entities", [])), "relations": len(analysis.get("relations", [])),
            "confidence": confidence,
        }

    def _ingest_analyze(self, title, content, source_type, existing_summaries, schema_text) -> dict:
        """Step 1: Extract entities, relations, key facts from source."""
        prompt = textwrap.dedent(f"""\
            You are a knowledge analyst. Extract structured knowledge from this source.

            Source: {title} (type: {source_type})

            Content:
            {content[:8000]}

            Existing wiki pages:
            {existing_summaries or "(none yet)"}

            Schema: {schema_text or "(none)"}

            Respond with ONLY JSON:
            {{
              "entities": [{{"name": "...", "type": "person|org|concept|tool|event|product", "description": "one line"}}],
              "relations": [{{"source": "entity A", "target": "entity B", "type": "uses|part_of|created_by|relates_to|competes_with", "weight": 0.8, "description": "..."}}],
              "key_facts": ["fact 1", "fact 2"],
              "category": "best category",
              "tags": ["tag1", "tag2"],
              "pages_to_update": ["slug"],
              "new_concepts": ["concept"],
              "supersedes": [{{"page": "existing-slug", "claim": "what old claim is now outdated", "replacement": "what the new info says"}}]
            }}

            For "supersedes": identify existing wiki pages whose claims are contradicted or made obsolete by this new source. Only flag clear factual supersessions, not minor differences.
        """)
        try:
            resp = self.router.ask(prompt, lane=self.lane, max_budget=0.25, timeout=90.0,
                                   evidence_pack={"step": "analyze", "source": title})
            return self._parse_json(resp.content)
        except Exception:
            logger.exception("Wiki ingest analysis failed for '%s'", title)
            return {"entities": [], "relations": [], "key_facts": [], "category": "Research",
                    "tags": ["research"], "pages_to_update": [], "new_concepts": []}

    def _ingest_generate(self, slug, title, content, analysis, existing_summaries, index_text, schema_text, now) -> dict:
        """Step 2: Generate wiki pages from analysis."""
        analysis_json = json.dumps(analysis, ensure_ascii=False)[:4000]
        prompt = textwrap.dedent(f"""\
            You are a wiki compiler. Generate pages based on this source analysis.

            Source: {title}
            Analysis: {analysis_json}
            Content (detail): {content[:5000]}
            Schema: {schema_text or "(none)"}
            Existing pages: {existing_summaries or "(none)"}
            Index: {index_text[:2000]}

            Respond with ONLY JSON:
            {{
              "summary_page": {{"filename": "{slug}.md", "content": "full markdown with ---frontmatter---"}},
              "updates": [{{"filename": "page.md", "content": "updated markdown"}}],
              "new_pages": [{{"filename": "concept.md", "content": "markdown"}}],
              "index_entries": [{{"category": "cat", "entry": "- [[{slug}]] — description"}}]
            }}

            Rules: [[wikilinks]] for cross-refs. Frontmatter: title, tags, category, sources, created ({now}), updated ({now}). Spanish by default. Only update pages from analysis.pages_to_update. Only create pages for analysis.new_concepts. ONLY valid JSON.
        """)
        try:
            resp = self.router.ask(prompt, lane=self.lane, max_budget=0.35, timeout=120.0,
                                   evidence_pack={"step": "generate", "source": title})
            return self._parse_json(resp.content)
        except Exception:
            logger.exception("Wiki ingest generation failed for '%s'", title)
            return {
                "summary_page": {"filename": f"{slug}.md",
                    "content": f"---\ntitle: {title}\ntags: [research]\nsources: [{slug}]\ncreated: {now}\nupdated: {now}\n---\n\n# {title}\n\n{content[:2000]}"},
                "updates": [], "new_pages": [],
                "index_entries": [{"category": "Research", "entry": f"- [[{slug}]] — {title}"}],
            }

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(self, question: str, *, archive: bool = False, token_budget: int = 6000) -> str:
        """Query the wiki. Optionally archive the answer as a new page.

        Args:
            token_budget: Approximate max tokens (~4 chars/token) for context pages.
        """
        index_text = self.index_path.read_text(encoding="utf-8") if self.index_path.exists() else ""

        # Find relevant pages from index + graph
        relevant = self._find_relevant_pages(question, index_text)

        # Read coherent relevant chunks within token budget.
        char_budget = token_budget * 4
        page_contents: list[str] = []
        chars_used = 0
        evidence_lines: list[str] = []
        pages_with_raw_evidence = 0
        for page_path in relevant[:8]:
            if not page_path.exists():
                continue
            text = page_path.read_text(encoding="utf-8")
            sources = self._extract_sources(text)
            has_raw_evidence = self._has_raw_evidence(sources)
            if has_raw_evidence:
                pages_with_raw_evidence += 1
            source_text = ", ".join(sources) if sources else "none"
            evidence_lines.append(
                f"- [[{page_path.stem}]] raw_evidence={'yes' if has_raw_evidence else 'no'} sources={source_text}"
            )
            available = char_budget - chars_used
            if available <= _MIN_CONTEXT_CHARS:
                break
            for chunk in self._select_context_chunks(question, text, max_chars=available):
                if chars_used + len(chunk) > char_budget:
                    chunk = chunk[: max(0, char_budget - chars_used)].strip()
                if len(chunk) <= _MIN_CONTEXT_CHARS:
                    continue
                page_contents.append(f"## {page_path.stem}\n{chunk}")
                chars_used += len(chunk)
                if char_budget - chars_used <= _MIN_CONTEXT_CHARS:
                    break
            if char_budget - chars_used <= _MIN_CONTEXT_CHARS:
                break

        if not page_contents:
            return ""

        context = "\n\n---\n\n".join(page_contents)
        evidence_status = (
            "sufficient"
            if pages_with_raw_evidence == len(evidence_lines) and evidence_lines
            else "partial"
            if pages_with_raw_evidence > 0
            else "insufficient"
        )

        prompt = textwrap.dedent(f"""\
            Answer the following question using ONLY the wiki pages provided.
            Cite sources using [[page-name]] wikilinks.
            If the wiki doesn't have enough information, say so clearly.
            Include a short confidence note: "Confianza: alta", "Confianza: media", or "Confianza: baja",
            based on whether the selected pages have raw evidence.

            Question: {question}

            Evidence status: {evidence_status}
            Evidence map:
            {chr(10).join(evidence_lines)}

            Wiki pages:
            {context}
        """)

        try:
            response = self.router.ask(
                prompt, lane=self.lane, max_budget=0.30, timeout=90.0,
                evidence_pack={"question": question, "pages": len(page_contents)},
            )
            answer = response.content.strip()
        except Exception:
            logger.exception("Wiki query failed for '%s'", question)
            return ""

        if evidence_status == "insufficient" and answer:
            answer = "Evidencia incompleta: las paginas wiki usadas no tienen fuentes raw verificables.\n\n" + answer
        elif evidence_status == "partial" and answer:
            answer = "Evidencia parcial: algunas paginas wiki usadas no tienen fuentes raw verificables.\n\n" + answer

        if archive and answer:
            slug = _slugify(f"query-{question[:40]}")
            now = _now_iso()
            page = (
                f"---\ntitle: Query — {question[:60]}\ntags: [research]\n"
                f"sources: [{', '.join(p.stem for p in relevant[:5])}]\n"
                f"created: {now}\nupdated: {now}\nconfidence: 0.2\n---\n\n{answer}"
            )
            (self.wiki_dir / f"{slug}.md").write_text(page, encoding="utf-8")
            self._update_index("Research", f"- [[{slug}]] — {question[:60]}")
            self._append_log("query_archived", question[:60], 1)

        return answer

    # ------------------------------------------------------------------
    # Lint
    # ------------------------------------------------------------------

    def lint(self) -> dict:
        """Health-check the wiki: orphans, missing pages, stale content."""
        pages = self._list_wiki_pages()
        if not pages:
            return {"orphans": [], "missing": [], "issues": 0}

        # Collect all outbound wikilinks and inbound links
        all_slugs = {p.stem for p in pages}
        inbound: dict[str, int] = {slug: 0 for slug in all_slugs}
        missing_links: set[str] = set()

        for page in pages:
            text = page.read_text(encoding="utf-8")
            links = _WIKILINK_RE.findall(text)
            for link in links:
                link_slug = _slugify(link)
                if link_slug in inbound:
                    inbound[link_slug] += 1
                else:
                    missing_links.add(link)

        orphans = [slug for slug, count in inbound.items() if count == 0]
        self._append_log("lint", f"pages={len(pages)} orphans={len(orphans)} missing={len(missing_links)}", 0)

        return {
            "total_pages": len(pages),
            "orphans": orphans,
            "missing": list(missing_links),
            "issues": len(orphans) + len(missing_links),
        }

    # ------------------------------------------------------------------
    # Deep Lint (LLM-powered)
    # ------------------------------------------------------------------

    def deep_lint(self, *, auto_fix: bool = False) -> dict:
        """LLM-powered audit: contradictions, stale content, concept gaps, research suggestions.

        Args:
            auto_fix: When True, automatically deprecate stale pages and create stubs for gaps.
        """
        pages = self._list_wiki_pages()
        if not pages:
            return {"contradictions": [], "stale": [], "gaps": [], "suggestions": [], "issues": 0}

        # Build a condensed snapshot of all pages (title + first 400 chars)
        page_summaries: list[str] = []
        for page in pages[:40]:  # cap to avoid token overflow
            try:
                text = page.read_text(encoding="utf-8")
                title = self._extract_title(page)
                # Extract frontmatter dates
                updated = ""
                m = _FRONTMATTER_RE.match(text)
                if m:
                    for line in m.group(1).splitlines():
                        if line.strip().startswith("updated:"):
                            updated = line.split(":", 1)[1].strip()
                body = text[text.find("---", 3) + 3:].strip() if "---" in text[3:] else text
                page_summaries.append(
                    f"### [[{page.stem}]] — {title}\n"
                    f"Updated: {updated or 'unknown'}\n"
                    f"{body[:400]}"
                )
            except Exception:
                continue

        if not page_summaries:
            return {"contradictions": [], "stale": [], "gaps": [], "suggestions": [], "issues": 0}

        # Run structural lint first
        structural = self.lint()

        wiki_snapshot = "\n\n".join(page_summaries)
        now = _now_iso()[:10]

        prompt = textwrap.dedent(f"""\
            You are a wiki auditor. Analyze the following wiki pages and identify issues.

            Today's date: {now}

            Structural issues already detected:
            - Orphan pages (no inbound links): {structural['orphans'][:10]}
            - Missing pages (linked but don't exist): {structural['missing'][:10]}

            Wiki pages snapshot:
            {wiki_snapshot}

            Analyze and respond with ONLY a JSON object:
            {{
              "contradictions": [
                {{"pages": ["page-a", "page-b"], "description": "what contradicts"}}
              ],
              "stale": [
                {{"page": "page-name", "reason": "why it seems outdated"}}
              ],
              "gaps": [
                {{"topic": "missing concept", "mentioned_in": ["page-a"], "description": "why it deserves a page"}}
              ],
              "suggestions": [
                {{"action": "research|merge|split|update", "target": "page or topic", "reason": "why"}}
              ]
            }}

            Rules:
            - Only flag real issues, not speculative ones.
            - A page is stale if its "updated" date is >60 days old AND the topic is fast-moving.
            - A contradiction is when two pages make incompatible claims about the same fact.
            - A gap is a concept referenced or implied across pages but lacking its own page.
            - Suggestions should be actionable: merge redundant pages, split overloaded ones, research new topics.
            - Output ONLY valid JSON, no explanation.
        """)

        try:
            response = self.router.ask(
                prompt, lane=self.lane, max_budget=0.40, timeout=120.0,
                evidence_pack={"operation": "deep_lint", "pages": len(pages)},
            )
            result = self._parse_json(response.content)
        except Exception:
            logger.exception("Wiki deep_lint LLM call failed")
            result = {"contradictions": [], "stale": [], "gaps": [], "suggestions": []}

        contradictions = result.get("contradictions", [])
        stale = result.get("stale", [])
        gaps = result.get("gaps", [])
        suggestions = result.get("suggestions", [])
        total_issues = len(contradictions) + len(stale) + len(gaps) + structural["issues"]

        # Self-curating: auto-fix stale pages and gap stubs
        auto_fixed: list[str] = []
        if auto_fix:
            now = _now_iso()
            for item in stale:
                page_slug = _slugify(item.get("page", ""))
                page_path = self.wiki_dir / f"{page_slug}.md"
                if page_path.exists():
                    self._set_frontmatter_field(page_path, "deprecated", "true")
                    auto_fixed.append(f"deprecated:{page_slug}")

            for gap in gaps:
                gap_slug = _slugify(gap.get("topic", ""))
                if not gap_slug:
                    continue
                gap_path = self.wiki_dir / f"{gap_slug}.md"
                if not gap_path.exists():
                    mentioned = ", ".join(f"[[{s}]]" for s in gap.get("mentioned_in", []))
                    stub = (
                        f"---\ntitle: {gap.get('topic', gap_slug)}\n"
                        f"tags: [stub, gap]\ncategory: Research\nsources: []\n"
                        f"created: {now}\nupdated: {now}\nconfidence: 0.1\n---\n\n"
                        f"# {gap.get('topic', gap_slug)}\n\n"
                        f"*Stub — this page was auto-created by deep_lint.*\n\n"
                        f"*Requires raw source evidence before it can be expanded.*\n\n"
                        f"{gap.get('description', '')}\n\n"
                        f"Mentioned in: {mentioned}\n"
                    )
                    gap_path.write_text(stub, encoding="utf-8")
                    self._index_page_embedding(gap_slug, stub)
                    self._update_index("Research", f"- [[{gap_slug}]] — {gap.get('topic', '')} (stub)")
                    auto_fixed.append(f"stub:{gap_slug}")

            if auto_fixed:
                self._save_embeddings()

        self._append_log(
            "deep_lint",
            f"pages={len(pages)} contradictions={len(contradictions)} stale={len(stale)} "
            f"gaps={len(gaps)} suggestions={len(suggestions)} auto_fixed={len(auto_fixed)}",
            len(auto_fixed),
        )

        return {
            **structural,
            "contradictions": contradictions,
            "stale": stale,
            "gaps": gaps,
            "suggestions": suggestions,
            "issues": total_issues,
            "auto_fixed": auto_fixed,
        }

    # ------------------------------------------------------------------
    # Auto-Research (periodic knowledge acquisition)
    # ------------------------------------------------------------------

    def auto_research(self, *, max_topics: int = 3) -> dict:
        """Identify knowledge gaps that need raw source research.

        Designed to run as a scheduled job (e.g. every 12 hours).
        This does not write wiki pages from LLM synthesis; candidates must be
        grounded through raw sources before they can become wiki truth.
        """
        pages = self._list_wiki_pages()
        if not pages:
            return {"topics_researched": 0, "pages_written": 0}

        existing = "\n".join(
            f"- [[{p.stem}]]: {self._extract_title(p)}" for p in pages[:30]
        )

        prompt = textwrap.dedent(f"""\
            You are a knowledge curator for an AI/tech wiki. Analyze the existing pages
            and suggest {max_topics} topics that are MISSING and would be valuable.

            Focus on: AI developments, frontier models, AI agents, AI safety,
            developer tools, and industry trends from 2026.

            Existing wiki pages:
            {existing}

            Respond with ONLY a JSON array of objects:
            [
              {{"topic": "short title", "category": "AI & Herramientas", "reason": "why this deserves research", "source_queries": ["specific source query"]}}
            ]

            Rules:
            - Only suggest topics NOT already covered by existing pages.
            - Do not write synthesized factual summaries.
            - Suggest concrete source queries that can produce raw evidence.
            - Write in Spanish.
            - ONLY valid JSON array.
        """)

        try:
            resp = self.router.ask(prompt, lane=self.lane, max_budget=0.40, timeout=120.0,
                                   evidence_pack={"operation": "auto_research"})
            topics = self._parse_json_array(resp.content)
        except Exception:
            logger.exception("Wiki auto_research failed")
            return {"topics_researched": 0, "pages_written": 0}

        candidates: list[dict] = []
        for topic in topics[:max_topics]:
            title = topic.get("topic", "")
            category = topic.get("category", "Research")
            if not title:
                continue
            slug = _slugify(title)
            if (self.wiki_dir / f"{slug}.md").exists():
                continue
            candidates.append({
                "topic": title,
                "slug": slug,
                "category": self._normalize_category(category),
                "reason": topic.get("reason", ""),
                "source_queries": topic.get("source_queries", []),
            })

        self._append_log("auto_research", f"topics={len(candidates)} candidates={len(candidates)} written=0", 0)
        return {"topics_researched": len(candidates), "pages_written": 0, "candidates": candidates}

    # ------------------------------------------------------------------
    # Auto-Scrape Sources
    # ------------------------------------------------------------------

    WATCH_SOURCES = [
        ("Crescendo AI News", "https://www.crescendo.ai/news/latest-ai-news-and-updates"),
        ("BuildEZ AI Trends", "https://www.buildez.ai/blog/ai-trending-april-2026-biggest-shifts"),
    ]

    def auto_scrape_sources(self) -> dict:
        """Scrape watched sources via firecrawl, extract key items, ingest new ones."""
        import subprocess
        now = time.time()
        if self._firecrawl_paused_until > now:
            remaining = int(self._firecrawl_paused_until - now)
            self._emit(
                "wiki_scrape_skipped",
                {
                    "reason": "firecrawl_paused",
                    "remaining_seconds": remaining,
                    "detail": self._firecrawl_pause_reason,
                },
            )
            return {
                "sources_scraped": 0,
                "pages_ingested": 0,
                "skipped": True,
                "reason": "firecrawl_paused",
                "remaining_seconds": remaining,
            }
        scraped = 0
        ingested = 0
        skipped = 0
        for name, url in self.WATCH_SOURCES:
            try:
                result = subprocess.run(
                    ["firecrawl", "scrape", url],
                    capture_output=True, text=True, timeout=60,
                )
                if result.returncode != 0 or not result.stdout.strip():
                    failure = classify_firecrawl_failure(result.stderr or result.stdout)
                    if failure == "insufficient_credits":
                        self._pause_firecrawl("insufficient_credits")
                        skipped += 1
                        break
                    if failure == "rate_limited":
                        self._pause_firecrawl("rate_limited", seconds=60 * 60)
                        skipped += 1
                        break
                    logger.warning("Wiki scrape failed for %s: %s", name, result.stderr[:200])
                    skipped += 1
                    continue
                scraped += 1
                content = result.stdout[:8000]
                # Use LLM to extract distinct news items
                prompt = textwrap.dedent(f"""\
                    Extract the 3 most important NEW items from this page content.
                    Source: {name} ({url})

                    Content:
                    {content}

                    Respond with ONLY a JSON array:
                    [{{"title": "short title in Spanish", "content": "2-3 paragraph summary in Spanish with facts and dates", "category": "AI & Herramientas"}}]

                    Rules:
                    - Only factual, specific items with dates and names.
                    - Write in Spanish.
                    - Skip opinion pieces or vague trend summaries.
                """)
                resp = self.router.ask(prompt, lane=self.lane, max_budget=0.30, timeout=90.0,
                                       evidence_pack={"operation": "auto_scrape", "source": name})
                items = self._parse_json_array(resp.content)
                for item in items[:3]:
                    title = item.get("title", "")
                    body = item.get("content", "")
                    if not title or len(body) < 50:
                        continue
                    slug = _slugify(title)
                    if (self.wiki_dir / f"{slug}.md").exists():
                        continue
                    if self._find_duplicate(body):
                        continue
                    source_content = f"Source: {name}\nURL: {url}\n\n{body}"
                    ingest_result = self.ingest(title, source_content, source_type="auto-scrape")
                    ingested += int(ingest_result.get("pages_written", 0))
            except Exception:
                logger.exception("Wiki scrape error for %s", name)
                skipped += 1
        self._append_log("auto_scrape", f"scraped={scraped} ingested={ingested}", ingested)
        return {"sources_scraped": scraped, "pages_ingested": ingested, "sources_skipped": skipped}

    def _pause_firecrawl(self, reason: str, *, seconds: int = 24 * 60 * 60) -> None:
        self._firecrawl_pause_reason = reason
        self._firecrawl_paused_until = time.time() + seconds
        logger.warning("Firecrawl auto-scrape paused: %s", reason)
        self._emit(
            "firecrawl_paused",
            {
                "reason": reason,
                "paused_seconds": seconds,
                "paused_until": self._firecrawl_paused_until,
            },
        )

    def _emit(self, event_type: str, payload: dict) -> None:
        if self.observe is None:
            return
        try:
            self.observe.emit(event_type, payload=payload)
        except Exception:
            logger.debug("Wiki observe emit failed", exc_info=True)

    # ------------------------------------------------------------------
    # NotebookLM → Wiki sync
    # ------------------------------------------------------------------

    def ingest_from_notebooklm(self, nlm_service: object, *, max_notebooks: int = 3, questions_per_nb: int = 2) -> dict:
        """Extract knowledge from NotebookLM notebooks and ingest into wiki.

        For each recent notebook, asks targeted questions via chat() to
        extract structured knowledge, then ingests unique findings.
        """
        try:
            notebooks = nlm_service.list_notebooks()  # type: ignore[union-attr]
        except Exception:
            logger.exception("Failed to list NotebookLM notebooks")
            return {"notebooks_scanned": 0, "pages_written": 0}

        if not notebooks:
            return {"notebooks_scanned": 0, "pages_written": 0}

        # Get existing wiki page titles to avoid asking about covered topics
        existing_titles = {self._extract_title(p).lower() for p in self._list_wiki_pages()}

        pages_written = 0
        notebooks_scanned = 0

        for nb in notebooks[:max_notebooks]:
            nb_id = nb["id"]
            nb_title = nb.get("title", nb_id[:8])
            notebooks_scanned += 1

            # Ask NotebookLM to extract key facts
            extraction_questions = [
                (
                    f"Lista los 3 hechos o desarrollos más importantes y específicos "
                    f"de este cuaderno. Para cada uno incluye: título corto, fechas, "
                    f"nombres de empresas/personas, y datos concretos. Responde en español."
                ),
                (
                    f"¿Qué tendencias emergentes o predicciones aparecen en las fuentes "
                    f"de este cuaderno que aún no son de conocimiento general? "
                    f"Responde con hechos específicos en español."
                ),
            ]

            for question in extraction_questions[:questions_per_nb]:
                try:
                    answer = nlm_service.chat(nb_id, question)  # type: ignore[union-attr]
                except Exception:
                    logger.debug("NotebookLM chat failed for %s", nb_id, exc_info=True)
                    continue

                if not answer or len(answer) < 100:
                    continue

                # Use LLM to structure the answer into wiki-ready items
                prompt = textwrap.dedent(f"""\
                    Extract distinct knowledge items from this NotebookLM response.
                    Source notebook: "{nb_title}"

                    Response:
                    {answer[:4000]}

                    Respond with ONLY a JSON array:
                    [{{"title": "short title in Spanish", "content": "2-3 paragraph summary in Spanish", "category": "AI & Herramientas"}}]

                    Rules:
                    - Each item must be self-contained with specific facts.
                    - Skip items that are vague or just opinions.
                    - Write in Spanish.
                """)

                try:
                    resp = self.router.ask(prompt, lane=self.lane, max_budget=0.30, timeout=90.0,
                                           evidence_pack={"operation": "nlm_wiki_sync", "notebook": nb_title})
                    items = self._parse_json_array(resp.content)
                except Exception:
                    logger.debug("LLM extraction failed for NLM response", exc_info=True)
                    continue

                for item in items[:3]:
                    title = item.get("title", "")
                    body = item.get("content", "")
                    if not title or len(body) < 50:
                        continue
                    if title.lower() in existing_titles:
                        continue
                    slug = _slugify(title)
                    if (self.wiki_dir / f"{slug}.md").exists():
                        continue
                    if self._find_duplicate(body):
                        continue
                    source_content = f"NotebookLM: {nb_title}\nNotebook ID: {nb_id}\n\n{body}"
                    ingest_result = self.ingest(title, source_content, source_type="notebooklm-sync")
                    if ingest_result.get("pages_written", 0):
                        existing_titles.add(title.lower())
                        pages_written += int(ingest_result.get("pages_written", 0))

        self._append_log("nlm_wiki_sync", f"notebooks={notebooks_scanned} written={pages_written}", pages_written)
        return {"notebooks_scanned": notebooks_scanned, "pages_written": pages_written}

    def _parse_json_array(self, text: str) -> list[dict]:
        """Parse a JSON array from LLM response."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)
        start = cleaned.find("[")
        end = cleaned.rfind("]") + 1
        if start == -1 or end == 0:
            return []
        return json.loads(cleaned[start:end])

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Return wiki statistics."""
        raw_count = len(list(self.raw_dir.glob("*.md")))
        wiki_count = len(list(self.wiki_dir.glob("*.md")))
        return {
            "raw_sources": raw_count,
            "wiki_pages": wiki_count,
            "wiki_root": str(self.root),
        }

    # ------------------------------------------------------------------
    # Cascade Delete
    # ------------------------------------------------------------------

    def delete(self, slug: str) -> dict:
        """Remove a source and cascade-clean wiki page, index, graph, and embeddings."""
        removed: list[str] = []

        # 1. Raw source
        raw = self.raw_dir / f"{slug}.md"
        if raw.exists():
            raw.unlink()
            removed.append(f"raw/{slug}.md")

        # 2. Wiki page
        wiki = self.wiki_dir / f"{slug}.md"
        if wiki.exists():
            wiki.unlink()
            removed.append(f"wiki/{slug}.md")

        # 3. Embeddings
        if slug in self._embeddings:
            del self._embeddings[slug]
            self._save_embeddings()
            removed.append("embedding")

        # 4. Graph: remove node and all edges referencing it
        if slug in self._graph:
            del self._graph[slug]
            removed.append("graph_node")
        for node, edges in list(self._graph.items()):
            before = len(edges)
            self._graph[node] = [e for e in edges if e.get("target") != slug and e.get("source_page") != slug]
            if len(self._graph[node]) < before:
                removed.append(f"graph_edges[{node}]")
        if self._graph:
            self._save_graph()

        # 5. Index: remove lines referencing [[slug]]
        if self.index_path.exists():
            lines = self.index_path.read_text(encoding="utf-8").splitlines()
            filtered = [l for l in lines if f"[[{slug}]]" not in l]
            if len(filtered) < len(lines):
                self.index_path.write_text("\n".join(filtered) + "\n", encoding="utf-8")
                removed.append("index_entry")

        self._append_log("delete", slug, len(removed))
        return {"slug": slug, "removed": removed}

    # ------------------------------------------------------------------
    # Confidence & time decay (Karpathy v2 upgrades)
    # ------------------------------------------------------------------

    def recompute_confidence(self) -> dict:
        """Recalculate confidence scores for all wiki pages. Designed for periodic scheduling."""
        pages = self._list_wiki_pages()
        updated = 0
        for page in pages:
            slug = page.stem
            score = self._compute_confidence(slug)
            self._set_frontmatter_field(page, "confidence", str(score))
            updated += 1
        self._append_log("recompute_confidence", f"pages={updated}", updated)
        return {"pages_updated": updated}

    def _extract_updated_date(self, page: Path) -> datetime | None:
        """Parse the `updated:` field from frontmatter."""
        try:
            text = page.read_text(encoding="utf-8")[:600]
            m = _FRONTMATTER_RE.match(text)
            if m:
                for line in m.group(1).splitlines():
                    if line.strip().startswith("updated:"):
                        val = line.split(":", 1)[1].strip().strip('"').strip("'")
                        parsed = datetime.fromisoformat(val)
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        return parsed
        except Exception:
            pass
        return None

    def _compute_confidence(self, slug: str) -> float:
        """Auto-calculate confidence score (0.0-1.0) based on source count, inbound links, and recency."""
        score = 0.3  # base

        # Source count: more edges referencing this slug = higher confidence
        inbound = sum(
            1 for edges in self._graph.values()
            for e in edges if e.get("target") == slug
        )
        score += min(inbound * 0.1, 0.3)  # cap at 0.3

        # Inbound wikilinks from other pages
        page_path = self.wiki_dir / f"{slug}.md"
        link_count = 0
        for p in self._list_wiki_pages()[:40]:
            if p.stem == slug:
                continue
            try:
                if f"[[{slug}]]" in p.read_text(encoding="utf-8")[:2000]:
                    link_count += 1
            except Exception:
                pass
        score += min(link_count * 0.05, 0.2)  # cap at 0.2

        # Recency bonus
        updated = self._extract_updated_date(page_path) if page_path.exists() else None
        if updated:
            days_old = (datetime.now(timezone.utc) - updated).days
            if days_old < 7:
                score += 0.2
            elif days_old < 30:
                score += 0.1
            elif days_old > 90:
                score -= 0.1

        return max(0.0, min(1.0, round(score, 2)))

    def _time_decay(self, page: Path) -> float:
        """Return a decay multiplier (0.5-1.0) based on page age.

        Pages tagged 'evergreen' are exempt from decay (always 1.0).
        """
        try:
            text = page.read_text(encoding="utf-8")[:600]
            m = _FRONTMATTER_RE.match(text)
            if m and "evergreen" in m.group(1):
                return 1.0
        except Exception:
            pass
        updated = self._extract_updated_date(page)
        if not updated:
            return 0.8
        days_old = max(0, (datetime.now(timezone.utc) - updated).days)
        return max(0.5, 1.0 / (1 + days_old / 180))

    def _set_frontmatter_field(self, page: Path, field: str, value: str) -> None:
        """Update or insert a single field in a page's YAML frontmatter."""
        try:
            text = page.read_text(encoding="utf-8")
        except Exception:
            return
        m = _FRONTMATTER_RE.match(text)
        if not m:
            return
        fm = m.group(1)
        body = text[m.end():]
        # Update or append field
        field_re = re.compile(rf"^{re.escape(field)}:.*$", re.MULTILINE)
        if field_re.search(fm):
            fm = field_re.sub(f"{field}: {value}", fm)
        else:
            fm += f"\n{field}: {value}"
        page.write_text(f"---\n{fm}\n---{body}", encoding="utf-8")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _list_wiki_pages(self) -> list[Path]:
        pages = sorted(self.wiki_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [p for p in pages if not self._is_deprecated(p)]

    @staticmethod
    def _is_deprecated(page: Path) -> bool:
        """Check if a page has deprecated: true in its frontmatter."""
        try:
            text = page.read_text(encoding="utf-8")[:500]
            m = _FRONTMATTER_RE.match(text)
            if m:
                for line in m.group(1).splitlines():
                    if line.strip().startswith("deprecated:") and "true" in line.lower():
                        return True
        except Exception:
            pass
        return False

    def _extract_title(self, page: Path) -> str:
        try:
            text = page.read_text(encoding="utf-8")[:500]
            m = _FRONTMATTER_RE.match(text)
            if m:
                for line in m.group(1).splitlines():
                    if line.startswith("title:"):
                        return line.split(":", 1)[1].strip()
            return page.stem
        except Exception:
            return page.stem

    def _find_relevant_pages(self, question: str, index_text: str) -> list[Path]:
        """Find wiki pages relevant to a question using hybrid retrieval + graph expansion."""
        ranked = self._rank_pages(question)
        scored: dict[str, tuple[float, Path]] = {
            item["slug"]: (float(item["score"]), item["page"]) for item in ranked if item["score"] > 0
        }

        top_slugs = [item["slug"] for item in ranked[:3]]
        for top_slug in top_slugs:
            top_score = scored.get(top_slug, (0.0, self.wiki_dir / f"{top_slug}.md"))[0]
            for neighbor in self._graph_neighbors(top_slug, depth=1):
                if neighbor not in scored:
                    npath = self.wiki_dir / f"{neighbor}.md"
                    if npath.exists():
                        scored[neighbor] = (top_score * 0.6, npath)

        if scored:
            return [p for _, p in sorted(scored.values(), key=lambda x: x[0], reverse=True)]
        return self._list_wiki_pages()[:3]

    # ------------------------------------------------------------------
    # Search (public, for tools and brain context)
    # ------------------------------------------------------------------

    def search(self, query: str, *, limit: int = 5) -> list[dict]:
        """Hybrid search across wiki pages. Returns semantic and keyword scores."""
        ranked = self._rank_pages(query)
        return [
            {
                "slug": item["slug"],
                "title": item["title"],
                "similarity": round(float(item["similarity"]), 4),
                "keyword_score": round(float(item["keyword_score"]), 4),
                "score": round(float(item["score"]), 4),
                "snippet": item["snippet"],
            }
            for item in ranked[:limit]
            if item["score"] > 0
        ]

    def _rank_pages(self, query: str) -> list[dict]:
        """Rank pages with BM25-style keyword retrieval plus cosine similarity."""
        pages = self._list_wiki_pages()
        if not pages or not query.strip():
            return []

        query_vec = _embed(query)
        query_tokens = _tokenize(query)
        page_records: list[dict] = []
        corpus_tokens: list[list[str]] = []
        embeddings_dirty = False
        for page in pages:
            slug = page.stem
            try:
                text = page.read_text(encoding="utf-8")
            except Exception:
                text = ""
            title = self._extract_title(page)
            search_text = f"{title} {slug.replace('-', ' ')} {text[:20000]}"
            page_vec = self._embeddings.get(slug)
            if page_vec is None:
                page_vec = _embed(search_text[:1500])
                self._embeddings[slug] = page_vec
                embeddings_dirty = True
            similarity = max(0.0, _cosine(query_vec, page_vec)) * self._time_decay(page)
            tokens = _tokenize(search_text)
            corpus_tokens.append(tokens)
            page_records.append({
                "slug": slug,
                "page": page,
                "title": title,
                "text": text,
                "similarity": similarity,
            })
        if embeddings_dirty:
            self._save_embeddings()

        keyword_scores = self._bm25_scores(query_tokens, corpus_tokens)
        max_keyword = max(keyword_scores) if keyword_scores else 0.0
        for idx, record in enumerate(page_records):
            raw_keyword = keyword_scores[idx] if idx < len(keyword_scores) else 0.0
            keyword_score = raw_keyword / max_keyword if max_keyword > 0 else 0.0
            if keyword_score <= 0 and query_tokens and idx < len(corpus_tokens):
                token_set = set(corpus_tokens[idx])
                keyword_score = len(set(query_tokens).intersection(token_set)) / max(len(set(query_tokens)), 1)
            similarity = float(record["similarity"])
            score = (similarity * 0.65) + (keyword_score * 0.35)
            record["keyword_score"] = keyword_score
            record["score"] = score
            record["snippet"] = self._best_snippet(query, record["text"])

        page_records.sort(key=lambda item: item["score"], reverse=True)
        return page_records

    @staticmethod
    def _bm25_scores(query_tokens: list[str], corpus_tokens: list[list[str]]) -> list[float]:
        if not query_tokens or not corpus_tokens:
            return [0.0 for _ in corpus_tokens]
        try:
            from rank_bm25 import BM25Okapi

            return [float(score) for score in BM25Okapi(corpus_tokens).get_scores(query_tokens)]
        except Exception:
            pass

        doc_count = len(corpus_tokens)
        avg_len = sum(len(doc) for doc in corpus_tokens) / doc_count if doc_count else 0.0
        doc_freq: dict[str, int] = {}
        for doc in corpus_tokens:
            for token in set(doc):
                doc_freq[token] = doc_freq.get(token, 0) + 1

        k1 = 1.5
        b = 0.75
        scores: list[float] = []
        for doc in corpus_tokens:
            if not doc:
                scores.append(0.0)
                continue
            term_counts: dict[str, int] = {}
            for token in doc:
                term_counts[token] = term_counts.get(token, 0) + 1
            score = 0.0
            for token in query_tokens:
                freq = term_counts.get(token, 0)
                if freq == 0:
                    continue
                df = doc_freq.get(token, 0)
                idf = math.log(1 + (doc_count - df + 0.5) / (df + 0.5))
                denom = freq + k1 * (1 - b + b * (len(doc) / (avg_len or 1.0)))
                score += idf * ((freq * (k1 + 1)) / denom)
            scores.append(score)
        return scores

    def _select_context_chunks(self, question: str, text: str, *, max_chars: int) -> list[str]:
        chunk_size = min(_DEFAULT_CHUNK_CHARS, max(_MIN_CONTEXT_CHARS, max_chars))
        chunks = self._split_text_chunks(text, max_chars=chunk_size, overlap=_CHUNK_OVERLAP_CHARS)
        if not chunks:
            return []
        query_vec = _embed(question)
        query_tokens = set(_tokenize(question))
        scored: list[tuple[float, int, str]] = []
        for idx, chunk in enumerate(chunks):
            chunk_tokens = _tokenize(chunk)
            keyword_hits = len(query_tokens.intersection(chunk_tokens))
            keyword_score = keyword_hits / max(len(query_tokens), 1)
            semantic_score = max(0.0, _cosine(query_vec, _embed(chunk[:1500])))
            scored.append(((semantic_score * 0.65) + (keyword_score * 0.35), idx, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)

        selected: list[tuple[int, str]] = []
        chars_used = 0
        for _, idx, chunk in scored:
            if chars_used + len(chunk) > max_chars and selected:
                continue
            selected.append((idx, chunk))
            chars_used += len(chunk)
            if chars_used >= max_chars:
                break
        selected.sort(key=lambda item: item[0])
        return [chunk for _, chunk in selected]

    def _split_text_chunks(self, text: str, *, max_chars: int, overlap: int = 0) -> list[str]:
        text = text.strip()
        if not text:
            return []
        chunks = self._recursive_split(text, max_chars, ["\n## ", "\n# ", "\n\n", ". ", " "])
        if overlap <= 0 or len(chunks) <= 1:
            return chunks
        overlapped: list[str] = []
        previous_tail = ""
        for chunk in chunks:
            merged = f"{previous_tail}{chunk}" if previous_tail else chunk
            overlapped.append(merged[:max_chars].strip())
            previous_tail = chunk[-overlap:] if len(chunk) > overlap else chunk
        return overlapped

    def _recursive_split(self, text: str, max_chars: int, separators: list[str]) -> list[str]:
        if len(text) <= max_chars:
            return [text.strip()]
        if not separators:
            return [text[i:i + max_chars].strip() for i in range(0, len(text), max_chars)]

        sep = separators[0]
        parts = text.split(sep)
        if len(parts) == 1:
            return self._recursive_split(text, max_chars, separators[1:])

        normalized: list[str] = []
        for idx, part in enumerate(parts):
            if not part:
                continue
            if idx > 0 and sep.strip():
                normalized.append(f"{sep.strip()} {part}" if sep.startswith("\n#") else part)
            else:
                normalized.append(part)

        chunks: list[str] = []
        current = ""
        joiner = sep if sep in ("\n\n", " ") else "\n"
        for part in normalized:
            candidate = f"{current}{joiner}{part}".strip() if current else part.strip()
            if len(candidate) <= max_chars:
                current = candidate
                continue
            if current:
                chunks.extend(self._recursive_split(current, max_chars, separators[1:]))
            current = part.strip()
        if current:
            chunks.extend(self._recursive_split(current, max_chars, separators[1:]))
        return [chunk for chunk in chunks if chunk]

    def _best_snippet(self, query: str, text: str, *, size: int = 300) -> str:
        if len(text) <= size:
            return text
        tokens = _tokenize(query)
        lower = text.lower()
        positions = [lower.find(token) for token in tokens if lower.find(token) >= 0]
        if not positions:
            return text[:size]
        start = max(0, min(positions) - 80)
        return text[start:start + size]

    def _ensure_raw_source(self, page: Path, raw_slug: str) -> None:
        try:
            text = page.read_text(encoding="utf-8")
        except Exception:
            return
        m = _FRONTMATTER_RE.match(text)
        if not m:
            return
        sources = self._extract_sources(text)
        if raw_slug not in sources:
            sources.append(raw_slug)
        fm = m.group(1)
        body = text[m.end():]
        source_line = f"sources: [{', '.join(sources)}]"
        if re.search(r"^sources:.*$", fm, flags=re.MULTILINE):
            fm = re.sub(r"^sources:.*(?:\n[ \t]+-.*)*", source_line, fm, flags=re.MULTILINE)
        else:
            fm += f"\n{source_line}"
        page.write_text(f"---\n{fm}\n---{body}", encoding="utf-8")

    def _extract_sources(self, text: str) -> list[str]:
        m = _FRONTMATTER_RE.match(text)
        if not m:
            return []
        lines = m.group(1).splitlines()
        sources: list[str] = []
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith("sources:"):
                continue
            value = stripped.split(":", 1)[1].strip()
            if value.startswith("[") and value.endswith("]"):
                return [self._clean_source(item) for item in value[1:-1].split(",") if self._clean_source(item)]
            if value:
                return [self._clean_source(value)]
            for follow in lines[idx + 1:]:
                if follow and not follow.startswith((" ", "\t")):
                    break
                item = follow.strip()
                if item.startswith("-"):
                    source = self._clean_source(item[1:].strip())
                    if source:
                        sources.append(source)
            return sources
        return []

    @staticmethod
    def _clean_source(value: str) -> str:
        return value.strip().strip('"').strip("'")

    def _has_raw_evidence(self, sources: list[str]) -> bool:
        for source in sources:
            slug = _slugify(source)
            candidates = [
                self.raw_dir / f"{source}.md",
                self.raw_dir / f"{slug}.md",
            ]
            if any(path.exists() for path in candidates):
                return True
        return False

    # ------------------------------------------------------------------
    # Embedding index persistence
    # ------------------------------------------------------------------

    def _load_embeddings(self) -> dict[str, list[float]]:
        if self._embeddings_path.exists():
            try:
                return json.loads(self._embeddings_path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("Failed to load wiki embeddings, starting fresh")
        return {}

    def _save_embeddings(self) -> None:
        with self._lock:
            try:
                import os
                tmp = self._embeddings_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._embeddings), encoding="utf-8")
                os.replace(tmp, self._embeddings_path)
            except Exception:
                logger.warning("Failed to save wiki embeddings")

    # ------------------------------------------------------------------
    # Knowledge Graph persistence
    # ------------------------------------------------------------------

    def _load_graph(self) -> dict[str, list[dict]]:
        if self._graph_path.exists():
            try:
                return json.loads(self._graph_path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("Failed to load wiki graph, starting fresh")
        return {}

    def _save_graph(self) -> None:
        with self._lock:
            try:
                import os
                tmp = self._graph_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._graph, ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(tmp, self._graph_path)
            except Exception:
                logger.warning("Failed to save wiki graph")

    def _update_graph_from_analysis(self, slug: str, analysis: dict) -> None:
        """Merge entities and relations from ingest analysis into the graph."""
        relations = analysis.get("relations", [])
        if not relations:
            return
        for rel in relations:
            src = _slugify(rel.get("source", ""))
            tgt = _slugify(rel.get("target", ""))
            if not src or not tgt:
                continue
            edge = {
                "target": tgt,
                "type": rel.get("type", "relates_to"),
                "weight": rel.get("weight", 0.5),
                "source_page": slug,
            }
            self._graph.setdefault(src, [])
            # Avoid duplicate edges
            if not any(e["target"] == tgt and e["source_page"] == slug for e in self._graph[src]):
                self._graph[src].append(edge)
            # Bidirectional (lower weight for reverse)
            rev = {
                "target": src,
                "type": rel.get("type", "relates_to"),
                "weight": rel.get("weight", 0.5) * 0.7,
                "source_page": slug,
            }
            self._graph.setdefault(tgt, [])
            if not any(e["target"] == src and e["source_page"] == slug for e in self._graph[tgt]):
                self._graph[tgt].append(rev)

    def _graph_neighbors(self, slug: str, *, depth: int = 1) -> list[str]:
        """Return neighboring slugs from the knowledge graph up to given depth."""
        visited: set[str] = set()
        frontier = {slug}
        for _ in range(depth):
            next_frontier: set[str] = set()
            for node in frontier:
                if node in visited:
                    continue
                visited.add(node)
                for edge in self._graph.get(node, []):
                    tgt = edge.get("target", "")
                    if tgt and tgt not in visited:
                        next_frontier.add(tgt)
            frontier = next_frontier
        return list(frontier - {slug})

    def _index_page_embedding(self, slug: str, content: str) -> None:
        """Create/update the embedding for a wiki page."""
        text = f"{slug.replace('-', ' ')} {content[:500]}"
        self._embeddings[slug] = _embed(text)

    def _update_index(self, category: str, entry: str) -> None:
        if not entry.strip():
            return
        category = self._normalize_category(category)
        text = self.index_path.read_text(encoding="utf-8") if self.index_path.exists() else ""
        # Check if entry already exists
        if entry.strip() in text:
            return
        # Find category section and append
        marker = f"## {category}"
        if marker in text:
            text = text.replace(marker, f"{marker}\n{entry.strip()}", 1)
        else:
            text += f"\n## {category}\n{entry.strip()}\n"
        self.index_path.write_text(text, encoding="utf-8")

    # ------------------------------------------------------------------
    # Dedup, category normalization, graph rebuild, raw backfill
    # ------------------------------------------------------------------

    def _find_duplicate(self, content: str, threshold: float = 0.85) -> str | None:
        """Check if content is too similar to an existing wiki page."""
        incoming_vec = _embed(content[:500])
        for slug, page_vec in self._embeddings.items():
            if _cosine(incoming_vec, page_vec) > threshold:
                return slug
        return None

    def _normalize_category(self, category: str) -> str:
        """Map LLM-assigned category to the closest valid category."""
        cat_lower = category.lower().strip()
        for valid in VALID_CATEGORIES:
            if cat_lower == valid.lower() or cat_lower in valid.lower() or valid.lower() in cat_lower:
                return valid
        return "Research"

    def rebuild_graph(self) -> dict:
        """Rebuild the knowledge graph from wikilinks in all wiki pages."""
        self._graph.clear()
        pages = self._list_wiki_pages()
        edges_added = 0
        for page in pages:
            slug = page.stem
            text = page.read_text(encoding="utf-8")
            links = _WIKILINK_RE.findall(text)
            for link in links:
                target = _slugify(link)
                if not target or target == slug:
                    continue
                edge = {
                    "target": target,
                    "type": "relates_to",
                    "weight": 0.5,
                    "source_page": slug,
                }
                self._graph.setdefault(slug, [])
                if not any(e["target"] == target for e in self._graph[slug]):
                    self._graph[slug].append(edge)
                    edges_added += 1
        self._save_graph()
        self._append_log("rebuild_graph", f"pages={len(pages)} edges={edges_added}", edges_added)
        return {"pages_scanned": len(pages), "edges": edges_added, "nodes": len(self._graph)}

    def backfill_raw(self) -> dict:
        """Create raw/ entries for wiki pages that lack corresponding raw files."""
        pages = self._list_wiki_pages()
        created = 0
        for page in pages:
            raw_path = self.raw_dir / f"{page.stem}.md"
            if not raw_path.exists():
                content = page.read_text(encoding="utf-8")
                title = self._extract_title(page)
                raw_path.write_text(
                    f"---\ntitle: {title}\ntype: backfill\ningested: {_now_iso()}\n---\n\n{content}",
                    encoding="utf-8",
                )
                created += 1
        self._append_log("backfill_raw", f"created={created}", created)
        return {"created": created}

    def _append_log(self, operation: str, title: str, pages_affected: int) -> None:
        now = _now_iso()[:10]
        entry = f"## [{now}] {operation} | {title} (pages: {pages_affected})\n"
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(entry)

    def _parse_json(self, text: str) -> dict:
        import json
        cleaned = text.strip()
        # Strip markdown fences
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError("No JSON object found in LLM response")
        return json.loads(cleaned[start:end])


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    import unicodedata
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "-", text).strip("-")[:80]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
