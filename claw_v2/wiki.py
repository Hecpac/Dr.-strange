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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claw_v2.llm import LLMRouter

logger = logging.getLogger(__name__)

_DEFAULT_WIKI_ROOT = Path.home() / ".claw" / "wiki"
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_FRONTMATTER_RE = re.compile(r"^---\n(.+?)\n---", re.DOTALL)

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
    ) -> None:
        self.router = router
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
        self._embeddings: dict[str, list[float]] = self._load_embeddings()
        self._graph: dict[str, list[dict]] = self._load_graph()

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
            (self.wiki_dir / summary["filename"]).write_text(summary["content"], encoding="utf-8")
            self._index_page_embedding(Path(summary["filename"]).stem, summary["content"])
            pages_written += 1

        for update in result.get("updates", []):
            if update.get("filename") and update.get("content"):
                target = self.wiki_dir / update["filename"]
                if target.exists():
                    target.write_text(update["content"], encoding="utf-8")
                    self._index_page_embedding(target.stem, update["content"])
                    pages_written += 1

        for new_page in result.get("new_pages", []):
            if new_page.get("filename") and new_page.get("content"):
                target = self.wiki_dir / new_page["filename"]
                if not target.exists():
                    target.write_text(new_page["content"], encoding="utf-8")
                    self._index_page_embedding(target.stem, new_page["content"])
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

        # Read relevant page contents within token budget
        char_budget = token_budget * 4
        page_contents: list[str] = []
        chars_used = 0
        for page_path in relevant[:8]:
            if not page_path.exists():
                continue
            text = page_path.read_text(encoding="utf-8")
            available = char_budget - chars_used
            if available <= 200:
                break
            chunk = text[:min(len(text), available)]
            page_contents.append(f"## {page_path.stem}\n{chunk}")
            chars_used += len(chunk)

        if not page_contents:
            return ""

        context = "\n\n---\n\n".join(page_contents)

        prompt = textwrap.dedent(f"""\
            Answer the following question using ONLY the wiki pages provided.
            Cite sources using [[page-name]] wikilinks.
            If the wiki doesn't have enough information, say so clearly.

            Question: {question}

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

        if archive and answer:
            slug = _slugify(f"query-{question[:40]}")
            now = _now_iso()
            page = (
                f"---\ntitle: Query — {question[:60]}\ntags: [research]\n"
                f"sources: [{', '.join(p.stem for p in relevant[:5])}]\n"
                f"created: {now}\nupdated: {now}\n---\n\n{answer}"
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
                        f"{gap.get('description', '')}\n\n"
                        f"Mentioned in: {mentioned}\n"
                    )
                    gap_path.write_text(stub, encoding="utf-8")
                    self._index_page_embedding(gap_slug, stub)
                    self._update_index("Research", f"- [[{gap_slug}]] — {gap.get('topic', '')} (stub)")
                    auto_fixed.append(f"stub:{gap_slug}")

            if auto_fixed:
                self._save_embeddings()

            # Auto-fill: enrich stubs with synthesized content from existing pages
            for fix in auto_fixed:
                if fix.startswith("stub:"):
                    stub_slug = fix[5:]
                    try:
                        answer = self.query(
                            f"¿Qué información tenemos sobre {stub_slug.replace('-', ' ')}?",
                            archive=False, token_budget=3000,
                        )
                        if answer and len(answer) > 100:
                            stub_path = self.wiki_dir / f"{stub_slug}.md"
                            if stub_path.exists():
                                text = stub_path.read_text(encoding="utf-8")
                                text += f"\n\n## Síntesis automática\n\n{answer}\n"
                                stub_path.write_text(text, encoding="utf-8")
                                self._index_page_embedding(stub_slug, text)
                                self._set_frontmatter_field(stub_path, "confidence", "0.3")
                    except Exception:
                        logger.debug("Auto-fill failed for stub %s", stub_slug, exc_info=True)

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
                        return datetime.fromisoformat(val)
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
        """Find wiki pages relevant to a question using semantic similarity + graph expansion."""
        query_vec = _embed(question)
        scored: dict[str, tuple[float, Path]] = {}
        for page in self._list_wiki_pages():
            slug = page.stem
            page_vec = self._embeddings.get(slug)
            if page_vec is None:
                text = f"{self._extract_title(page)} {slug.replace('-', ' ')}"
                try:
                    content = page.read_text(encoding="utf-8")[:200]
                    text += f" {content}"
                except Exception:
                    pass
                page_vec = _embed(text)
                self._embeddings[slug] = page_vec
                self._save_embeddings()
            sim = _cosine(query_vec, page_vec)
            if sim > 0.15:
                # Apply time decay: newer pages rank higher
                sim *= self._time_decay(page)
                scored[slug] = (sim, page)

        # Graph expansion: boost neighbors of top hits
        top_slugs = sorted(scored, key=lambda s: scored[s][0], reverse=True)[:3]
        for top_slug in top_slugs:
            for neighbor in self._graph_neighbors(top_slug, depth=1):
                if neighbor not in scored:
                    npath = self.wiki_dir / f"{neighbor}.md"
                    if npath.exists():
                        scored[neighbor] = (scored[top_slug][0] * 0.6, npath)

        ranked = sorted(scored.values(), key=lambda x: x[0], reverse=True)
        if ranked:
            return [p for _, p in ranked]
        return self._list_wiki_pages()[:3]

    # ------------------------------------------------------------------
    # Search (public, for tools and brain context)
    # ------------------------------------------------------------------

    def search(self, query: str, *, limit: int = 5) -> list[dict]:
        """Semantic search across wiki pages. Returns [{slug, title, similarity, snippet}]."""
        query_vec = _embed(query)
        scored: list[tuple[float, Path]] = []
        for page in self._list_wiki_pages():
            slug = page.stem
            page_vec = self._embeddings.get(slug)
            if page_vec is None:
                text = f"{self._extract_title(page)} {slug.replace('-', ' ')}"
                try:
                    text += f" {page.read_text(encoding='utf-8')[:200]}"
                except Exception:
                    pass
                page_vec = _embed(text)
                self._embeddings[slug] = page_vec
            sim = _cosine(query_vec, page_vec)
            if sim > 0.15:
                scored.append((sim, page))
        if scored:
            self._save_embeddings()
        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for sim, page in scored[:limit]:
            try:
                snippet = page.read_text(encoding="utf-8")[:300]
            except Exception:
                snippet = ""
            results.append({
                "slug": page.stem,
                "title": self._extract_title(page),
                "similarity": round(sim, 4),
                "snippet": snippet,
            })
        return results

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
        try:
            self._embeddings_path.write_text(
                json.dumps(self._embeddings), encoding="utf-8"
            )
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
        try:
            self._graph_path.write_text(
                json.dumps(self._graph, ensure_ascii=False, indent=1), encoding="utf-8"
            )
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
