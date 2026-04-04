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
from datetime import datetime, timezone
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
        self.lane = lane
        # Ensure dirs exist
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        # In-memory embedding index: {slug: [float, ...]}
        self._embeddings: dict[str, list[float]] = self._load_embeddings()

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    def ingest(self, title: str, content: str, *, source_type: str = "article") -> dict:
        """Ingest a new source: save raw, compile wiki pages, update index & log."""
        slug = _slugify(title)
        now = _now_iso()

        # 1. Save raw source (immutable)
        raw_path = self.raw_dir / f"{slug}.md"
        raw_path.write_text(
            f"---\ntitle: {title}\ntype: {source_type}\ningested: {now}\n---\n\n{content}",
            encoding="utf-8",
        )

        # 2. Read current index for context
        index_text = self.index_path.read_text(encoding="utf-8") if self.index_path.exists() else ""

        # 3. Read existing wiki pages for cross-reference context
        existing_pages = self._list_wiki_pages()
        existing_summaries = "\n".join(
            f"- [[{p.stem}]]: {self._extract_title(p)}" for p in existing_pages[:30]
        )

        # 4. Load schema for domain context
        schema_text = ""
        if self.schema_path.exists():
            try:
                schema_text = self.schema_path.read_text(encoding="utf-8")[:3000]
            except Exception:
                pass

        # 5. Ask LLM to compile wiki pages
        prompt = textwrap.dedent(f"""\
            You are a wiki compiler. A new source has been ingested. Your job:

            1. Write a summary page for this source as markdown with YAML frontmatter.
            2. List any existing wiki pages that should be updated with new information from this source.
            3. For each page to update, provide the updated content.
            4. Suggest any NEW concept/entity pages that should be created.

            Wiki schema (categories, tags, conventions):
            {schema_text or "(no schema defined — use your best judgment)"}

            Source title: {title}
            Source type: {source_type}

            Source content:
            {content[:8000]}

            Existing wiki pages:
            {existing_summaries or "(none yet)"}

            Current index:
            {index_text[:2000]}

            Respond with a JSON object:
            {{
              "summary_page": {{
                "filename": "{slug}.md",
                "content": "full markdown with ---frontmatter--- including title, tags, category, sources, created, updated"
              }},
              "updates": [
                {{"filename": "existing-page.md", "content": "full updated markdown"}}
              ],
              "new_pages": [
                {{"filename": "new-concept.md", "content": "full markdown with frontmatter"}}
              ],
              "index_entries": [
                {{"category": "matching category from schema or new one", "entry": "- [[{slug}]] — one-line description"}}
              ]
            }}

            Rules:
            - Follow the schema conventions strictly.
            - Use [[page-name]] wikilinks for cross-references.
            - Frontmatter must include: title, tags, category, sources, created, updated.
            - Use tags from the schema. Create new tags only if none fit.
            - Use category names from the schema index. Create new categories only if the source clearly doesn't fit any.
            - Pages in Spanish by default. English for highly technical content.
            - Keep pages concise but self-contained.
            - Output ONLY valid JSON, no explanation.
        """)

        try:
            response = self.router.ask(prompt, lane=self.lane, max_budget=0.50, timeout=120.0)
            result = self._parse_json(response.content)
        except Exception:
            logger.exception("Wiki ingest LLM call failed for '%s'", title)
            # Minimal fallback: just save a basic summary page
            result = {
                "summary_page": {
                    "filename": f"{slug}.md",
                    "content": f"---\ntitle: {title}\ntags: [research]\nsources: [{slug}]\ncreated: {now}\nupdated: {now}\n---\n\n# {title}\n\n{content[:2000]}",
                },
                "updates": [],
                "new_pages": [],
                "index_entries": [{"category": "Research", "entry": f"- [[{slug}]] — {title}"}],
            }

        pages_written = 0

        # 5. Write summary page
        summary = result.get("summary_page", {})
        if summary.get("content"):
            (self.wiki_dir / summary["filename"]).write_text(summary["content"], encoding="utf-8")
            self._index_page_embedding(Path(summary["filename"]).stem, summary["content"])
            pages_written += 1

        # 6. Write updates to existing pages
        for update in result.get("updates", []):
            if update.get("filename") and update.get("content"):
                target = self.wiki_dir / update["filename"]
                if target.exists():
                    target.write_text(update["content"], encoding="utf-8")
                    self._index_page_embedding(target.stem, update["content"])
                    pages_written += 1

        # 7. Write new pages
        for new_page in result.get("new_pages", []):
            if new_page.get("filename") and new_page.get("content"):
                target = self.wiki_dir / new_page["filename"]
                if not target.exists():
                    target.write_text(new_page["content"], encoding="utf-8")
                    self._index_page_embedding(target.stem, new_page["content"])
                    pages_written += 1

        # 8. Update index
        for entry in result.get("index_entries", []):
            self._update_index(entry.get("category", "Research"), entry.get("entry", ""))

        # 9. Persist embeddings & append to log
        self._save_embeddings()
        self._append_log("ingest", title, pages_written)

        return {
            "slug": slug,
            "raw_path": str(raw_path),
            "pages_written": pages_written,
            "updates": len(result.get("updates", [])),
            "new_pages": len(result.get("new_pages", [])),
        }

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(self, question: str, *, archive: bool = False) -> str:
        """Query the wiki. Optionally archive the answer as a new page."""
        index_text = self.index_path.read_text(encoding="utf-8") if self.index_path.exists() else ""

        # Find relevant pages from index
        relevant = self._find_relevant_pages(question, index_text)

        # Read relevant page contents
        page_contents = []
        for page_path in relevant[:5]:
            if page_path.exists():
                page_contents.append(f"## {page_path.stem}\n{page_path.read_text(encoding='utf-8')[:3000]}")

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
            response = self.router.ask(prompt, lane=self.lane, max_budget=0.30, timeout=90.0)
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
    # Internal helpers
    # ------------------------------------------------------------------

    def _list_wiki_pages(self) -> list[Path]:
        return sorted(self.wiki_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)

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
        """Find wiki pages relevant to a question using semantic similarity."""
        query_vec = _embed(question)
        scored: list[tuple[float, Path]] = []
        for page in self._list_wiki_pages():
            slug = page.stem
            page_vec = self._embeddings.get(slug)
            if page_vec is None:
                # Index on the fly from title + first 200 chars
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
                scored.append((sim, page))
        scored.sort(key=lambda x: x[0], reverse=True)
        if scored:
            return [p for _, p in scored]
        # Fallback: most recent pages
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
