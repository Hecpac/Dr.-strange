# GraphRAG in `memory.py` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade `MemoryStore.search_facts_semantic` and `search_outcomes_semantic` from pure cosine similarity to hybrid retrieval (BM25 + cosine) plus entity-graph expansion, by porting the proven pattern from `claw_v2/wiki.py` (`_bm25_scores`, `_graph_neighbors`, hybrid scoring at line 1115). Result: `LearningLoop.retrieve_lessons` recovers lessons whose embeddings don't match the query but which share entity tags with high-similarity hits.

**Architecture:** Three SQLite-resident additions to the existing `MemoryStore`. (1) A pure-function BM25 scorer next to `_cosine_similarity`, with optional `rank_bm25` acceleration. (2) An `outcome_entity_edges` table mapping `outcome_id → entity_tag`, populated on write from the existing `task_outcomes.tags` JSON column and backfilled lazily on `MemoryStore.__init__`. (3) A graph-expanded `search_outcomes_with_graph(query, ...)` that runs hybrid retrieval, takes top-K results, expands via shared entity tags up to depth=1, and merges neighbor outcomes with a discount factor (0.6× the seed's score, mirroring `wiki.py:1046`). `LearningLoop.retrieve_lessons` calls the new method by default. Facts get the hybrid upgrade only — graph expansion stays scoped to outcomes (where lesson-similar-failures-via-shared-entities is the actual use case; profile facts are too small to benefit).

**Tech Stack:** Python 3.12 stdlib (`sqlite3`, `math`, `re`), optional `rank_bm25` if installed (already imported in `wiki.py:1128`), `unittest.TestCase` (project convention — see `tests/test_memory_core.py`, `tests/test_memory_scoped.py`).

**Spec:** Direct port of the pattern at `claw_v2/wiki.py:1072-1161` (`_rank_pages` + `_bm25_scores`) and `claw_v2/wiki.py:1388-1403` (`_graph_neighbors`). Audit confirmed the pattern is production-tested in wiki and missing in memory (see audit run on 2026-04-19).

**Non-goals (explicit scope fences):**
- No graph expansion for `search_facts_semantic` (use case mismatch — profile facts).
- No LLM-based entity/relation extraction (we reuse the existing `tags` column; no new LLM calls).
- No external graph DB (Neo4j etc.) — pure SQLite.
- No changes to `wiki.py` (already correct; we're catching memory.py up).
- No changes to embedding model or dimensions.
- No new public API surface beyond `search_outcomes_with_graph`; `search_facts_semantic` and `search_outcomes_semantic` keep their signatures, only their internals change.

---

### Task 1: Add `_tokenize` and `_bm25_scores` module-level helpers

**Files:**
- Modify: `claw_v2/memory.py` (add helpers near `_cosine_similarity` at line 114)
- Test: `tests/test_memory_core.py` (new `BM25HelperTests` class at end of file)

**Rationale:** BM25 is a pure function over token lists. Putting it at module scope (not in `MemoryStore`) matches the `_cosine_similarity` placement and lets us test it without DB setup. The `rank_bm25` import is optional with a manual-formula fallback (mirrors `wiki.py:1124-1161`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_core.py — append to end of file
class BM25HelperTests(unittest.TestCase):
    def test_tokenize_lowercases_and_splits(self) -> None:
        from claw_v2.memory import _tokenize
        self.assertEqual(_tokenize("Hello World-Foo"), ["hello", "world-foo"])

    def test_tokenize_drops_punctuation(self) -> None:
        from claw_v2.memory import _tokenize
        self.assertEqual(_tokenize("foo, bar! baz?"), ["foo", "bar", "baz"])

    def test_bm25_empty_query_returns_zeros(self) -> None:
        from claw_v2.memory import _bm25_scores
        scores = _bm25_scores([], [["doc", "one"], ["doc", "two"]])
        self.assertEqual(scores, [0.0, 0.0])

    def test_bm25_empty_corpus_returns_empty(self) -> None:
        from claw_v2.memory import _bm25_scores
        self.assertEqual(_bm25_scores(["q"], []), [])

    def test_bm25_ranks_matching_doc_higher(self) -> None:
        from claw_v2.memory import _bm25_scores
        corpus = [["python", "import", "error"], ["unrelated", "text", "here"]]
        scores = _bm25_scores(["python", "import"], corpus)
        self.assertGreater(scores[0], scores[1])
        self.assertGreater(scores[0], 0.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_memory_core.py::BM25HelperTests -v`
Expected: 5 errors, all `ImportError: cannot import name '_tokenize'` / `'_bm25_scores'`.

- [ ] **Step 3: Add helpers in `claw_v2/memory.py`**

Insert this block immediately after the `_cosine_similarity` function (after line 120, before the `_ST_MODEL` declaration at line 123):

```python
import re as _re_for_tokenize
_TOKEN_RE = _re_for_tokenize.compile(r"[\w][\w-]*", _re_for_tokenize.IGNORECASE)


def _tokenize(text: str) -> list[str]:
    """Lowercase token splitter for BM25 — mirrors wiki.py:_tokenize."""
    return [tok.lower() for tok in _TOKEN_RE.findall(text)]


def _bm25_scores(query_tokens: list[str], corpus_tokens: list[list[str]]) -> list[float]:
    """BM25 scores for each document. Uses rank_bm25 if available; else manual formula.

    Mirrors claw_v2/wiki.py:1124-1161 verbatim — keep them in sync if you change either.
    """
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
```

Note: the existing `import math` at line 6 and the file-level imports cover the needs; the local `import re as _re_for_tokenize` avoids conflicting with any future `re` import.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_memory_core.py::BM25HelperTests -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_memory_core.py claw_v2/memory.py
git commit -m "feat(memory): add BM25 + tokenize helpers (Task 1/10 GraphRAG)"
```

---

### Task 2: Hybrid retrieval in `search_facts_semantic`

**Files:**
- Modify: `claw_v2/memory.py` (`search_facts_semantic` at line 875-918)
- Test: `tests/test_memory_core.py` (new `HybridFactSearchTests` class)

**Rationale:** Pure cosine misses facts with strong keyword overlap but weak semantic similarity (e.g. exact entity names that the embedding model treats as generic). Hybrid (0.65 cosine + 0.35 BM25, normalized) recovers them. Same weights as `wiki.py:1115`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_core.py — append
class HybridFactSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore(Path(tempfile.mkdtemp()) / "test.db")

    def test_keyword_overlap_boosts_score(self) -> None:
        # Both facts get an embedding, but the second has the exact query token in its key.
        self.store.store_fact_with_embedding(
            "general.preference", "user likes dark interfaces and minimal UI",
            source="profile", confidence=0.5,
        )
        self.store.store_fact_with_embedding(
            "tradingview.session", "TradingView session id renews every 24h",
            source="profile", confidence=0.5,
        )
        results = self.store.search_facts_semantic("tradingview", limit=2)
        self.assertGreaterEqual(len(results), 1)
        # The exact-match fact should rank first, even if the embedding for the other
        # vague fact happens to be close.
        self.assertEqual(results[0]["key"], "tradingview.session")

    def test_pure_semantic_match_still_works(self) -> None:
        self.store.store_fact_with_embedding(
            "weather.note", "It rained heavily yesterday",
            source="user", confidence=0.5,
        )
        results = self.store.search_facts_semantic("storm", limit=1)
        # Semantic similarity should still catch this even with no keyword overlap.
        self.assertEqual(len(results), 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_memory_core.py::HybridFactSearchTests -v`
Expected: `test_keyword_overlap_boosts_score` likely fails (current ranking is cosine-only, the boost from keyword overlap doesn't exist yet). `test_pure_semantic_match_still_works` likely passes already — that's fine.

- [ ] **Step 3: Replace the body of `search_facts_semantic`**

Open `claw_v2/memory.py`. Find the existing method at line 875:

```python
    def search_facts_semantic(
        self,
        query: str,
        limit: int = 10,
        min_similarity: float = 0.1,
        embed_fn: Callable[..., list[float]] | None = None,
    ) -> list[dict]:
```

Replace its body (lines 882-918) with:

```python
        embedder = embed_fn or _simple_embedding
        query_vec = embedder(query)
        query_dim = len(query_vec)
        query_tokens = _tokenize(query)
        rows = self._conn.execute(
            """
            SELECT f.id, f.key, f.value, f.source, f.source_trust, f.confidence, fe.embedding
            FROM facts f
            JOIN fact_embeddings fe ON f.id = fe.fact_id
            """,
        ).fetchall()
        if not rows:
            return []

        candidates: list[dict] = []
        corpus_tokens: list[list[str]] = []
        stale_ids: list[tuple[int, str, str]] = []
        for row in rows:
            stored_vec = json.loads(row["embedding"])
            if len(stored_vec) != query_dim:
                stored_vec = embedder(f"{row['key']} {row['value']}")
                stale_ids.append((row["id"], row["key"], row["value"]))
            sim = _cosine_similarity(query_vec, stored_vec)
            tokens = _tokenize(f"{row['key']} {row['value']}")
            corpus_tokens.append(tokens)
            candidates.append({
                "id": row["id"], "key": row["key"], "value": row["value"],
                "source": row["source"], "source_trust": row["source_trust"],
                "confidence": row["confidence"], "similarity_raw": sim,
            })

        keyword_scores = _bm25_scores(query_tokens, corpus_tokens)
        max_keyword = max(keyword_scores) if keyword_scores else 0.0
        scored: list[dict] = []
        for idx, item in enumerate(candidates):
            sim = max(0.0, item["similarity_raw"])
            raw_kw = keyword_scores[idx] if idx < len(keyword_scores) else 0.0
            kw_norm = raw_kw / max_keyword if max_keyword > 0 else 0.0
            score = (sim * 0.65) + (kw_norm * 0.35)
            if sim < min_similarity and kw_norm == 0.0:
                continue
            scored.append({
                "key": item["key"], "value": item["value"],
                "source": item["source"], "confidence": item["confidence"],
                "similarity": round(sim, 4),
                "keyword_score": round(kw_norm, 4),
                "score": round(score, 4),
            })

        if stale_ids:
            with self._lock:
                for fact_id, key, value in stale_ids:
                    new_vec = embedder(f"{key} {value}")
                    self._conn.execute(
                        "UPDATE fact_embeddings SET embedding = ? WHERE fact_id = ?",
                        (json.dumps(new_vec), fact_id),
                    )
                self._conn.commit()

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]
```

- [ ] **Step 4: Run all memory tests to verify nothing regressed**

Run: `python -m pytest tests/test_memory_core.py tests/test_memory_scoped.py -v`
Expected: all pass, including the two new `HybridFactSearchTests` cases.

- [ ] **Step 5: Commit**

```bash
git add tests/test_memory_core.py claw_v2/memory.py
git commit -m "feat(memory): hybrid (BM25+cosine) search for facts (Task 2/10 GraphRAG)"
```

---

### Task 3: Hybrid retrieval in `search_outcomes_semantic`

**Files:**
- Modify: `claw_v2/memory.py` (`search_outcomes_semantic` at line 1091-1138)
- Test: `tests/test_memory_core.py` (new `HybridOutcomeSearchTests` class)

**Rationale:** Same hybrid pattern as Task 2, applied to outcomes. The text indexed for BM25 is the same string used for the embedding (`description | approach | lesson [| error_snippet]`), keeping the two signals aligned.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_core.py — append
class HybridOutcomeSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore(Path(tempfile.mkdtemp()) / "test.db")

    def test_outcome_keyword_overlap_boosts_rank(self) -> None:
        self.store.store_task_outcome_with_embedding(
            task_type="browse", task_id="s1:1",
            description="Generic browsing failure", approach="default strategy",
            outcome="failure", lesson="Try fallback transport",
            error_snippet="connection refused",
        )
        self.store.store_task_outcome_with_embedding(
            task_type="browse", task_id="s1:2",
            description="Tradingview chart capture failed", approach="cdp_browser",
            outcome="failure", lesson="Tradingview needs explicit user-data-dir",
            error_snippet="cdp connect timeout",
        )
        results = self.store.search_outcomes_semantic("tradingview", limit=2)
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0]["task_id"], "s1:2")

    def test_results_include_hybrid_score_field(self) -> None:
        self.store.store_task_outcome_with_embedding(
            task_type="browse", task_id="s1:1",
            description="Search the web", approach="firecrawl",
            outcome="success", lesson="Firecrawl is reliable for static pages",
        )
        results = self.store.search_outcomes_semantic("firecrawl", limit=1)
        self.assertEqual(len(results), 1)
        self.assertIn("score", results[0])
        self.assertIn("keyword_score", results[0])
        self.assertIn("similarity", results[0])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_memory_core.py::HybridOutcomeSearchTests -v`
Expected: `test_results_include_hybrid_score_field` fails on `assertIn("keyword_score", ...)` — current method only returns `similarity`.

- [ ] **Step 3: Replace `search_outcomes_semantic` body**

Find the method at line 1091-1138 in `claw_v2/memory.py`. Replace its body with:

```python
        embedder = embed_fn or _simple_embedding
        query_vec = embedder(query)
        query_dim = len(query_vec)
        query_tokens = _tokenize(query)
        base_sql = (
            "SELECT o.id, o.task_type, o.task_id, o.description, o.approach, "
            "o.outcome, o.lesson, o.error_snippet, o.retries, o.created_at, "
            "o.feedback, oe.embedding "
            "FROM task_outcomes o JOIN outcome_embeddings oe ON o.id = oe.outcome_id"
        )
        if task_type:
            rows = self._conn.execute(base_sql + " WHERE o.task_type = ?", (task_type,)).fetchall()
        else:
            rows = self._conn.execute(base_sql).fetchall()
        if not rows:
            return []

        candidates: list[dict] = []
        corpus_tokens: list[list[str]] = []
        stale: list[tuple[int, str]] = []
        for row in rows:
            stored_vec = json.loads(row["embedding"])
            text = f"{row['description']} | {row['approach']} | {row['lesson']}"
            if row["error_snippet"]:
                text += f" | {row['error_snippet']}"
            if len(stored_vec) != query_dim:
                stored_vec = embedder(text)
                stale.append((row["id"], text))
            sim = _cosine_similarity(query_vec, stored_vec)
            corpus_tokens.append(_tokenize(text))
            item = dict(row)
            item.pop("embedding", None)
            item["similarity_raw"] = sim
            candidates.append(item)

        keyword_scores = _bm25_scores(query_tokens, corpus_tokens)
        max_keyword = max(keyword_scores) if keyword_scores else 0.0
        scored: list[dict] = []
        for idx, item in enumerate(candidates):
            sim = max(0.0, item.pop("similarity_raw"))
            raw_kw = keyword_scores[idx] if idx < len(keyword_scores) else 0.0
            kw_norm = raw_kw / max_keyword if max_keyword > 0 else 0.0
            score = (sim * 0.65) + (kw_norm * 0.35)
            if sim < min_similarity and kw_norm == 0.0:
                continue
            item["similarity"] = round(sim, 4)
            item["keyword_score"] = round(kw_norm, 4)
            item["score"] = round(score, 4)
            scored.append(item)

        if stale:
            with self._lock:
                for oid, text in stale:
                    self._conn.execute(
                        "UPDATE outcome_embeddings SET embedding = ? WHERE outcome_id = ?",
                        (json.dumps(embedder(text)), oid),
                    )
                self._conn.commit()

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_memory_core.py tests/test_memory_scoped.py tests/test_brain_core.py -v`
Expected: all pass. (`test_brain_core` is included because LearningLoop calls `search_outcomes_semantic` indirectly.)

- [ ] **Step 5: Commit**

```bash
git add tests/test_memory_core.py claw_v2/memory.py
git commit -m "feat(memory): hybrid (BM25+cosine) search for outcomes (Task 3/10 GraphRAG)"
```

---

### Task 4: `outcome_entity_edges` table + migration

**Files:**
- Modify: `claw_v2/memory.py` (SCHEMA at line 16-111, `_migrate` at line 236-318)
- Test: `tests/test_memory_core.py` (new `EntityEdgesSchemaTests` class)

**Rationale:** Graph expansion needs an indexable mapping `(outcome_id, entity_tag)`. Storing it as a separate table (rather than parsing `task_outcomes.tags` JSON on every search) keeps reads cheap. Mirror the existing `outcome_embeddings` migration shape (line 262-274).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_core.py — append
class EntityEdgesSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = Path(tempfile.mkdtemp()) / "test.db"
        self.store = MemoryStore(self.db_path)

    def test_outcome_entity_edges_table_exists(self) -> None:
        cursor = self.store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='outcome_entity_edges'"
        )
        self.assertIsNotNone(cursor.fetchone())

    def test_outcome_entity_edges_columns(self) -> None:
        cursor = self.store._conn.execute("PRAGMA table_info(outcome_entity_edges)")
        cols = {row[1] for row in cursor.fetchall()}
        self.assertEqual(cols, {"outcome_id", "entity_tag"})

    def test_outcome_entity_edges_index_exists(self) -> None:
        cursor = self.store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_outcome_entity_tag'"
        )
        self.assertIsNotNone(cursor.fetchone())

    def test_migration_idempotent_on_reopen(self) -> None:
        store2 = MemoryStore(self.db_path)
        cursor = store2._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='outcome_entity_edges'"
        )
        self.assertIsNotNone(cursor.fetchone())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_memory_core.py::EntityEdgesSchemaTests -v`
Expected: 4 failures, all `AssertionError` because the table doesn't exist yet.

- [ ] **Step 3: Add table to SCHEMA constant**

In `claw_v2/memory.py`, append to the `SCHEMA` string (just before the closing `"""` at line 111, after the existing index lines):

```python
CREATE TABLE IF NOT EXISTS outcome_entity_edges (
    outcome_id INTEGER NOT NULL REFERENCES task_outcomes(id) ON DELETE CASCADE,
    entity_tag TEXT NOT NULL,
    PRIMARY KEY (outcome_id, entity_tag)
);

CREATE INDEX IF NOT EXISTS idx_outcome_entity_tag
    ON outcome_entity_edges(entity_tag);
```

- [ ] **Step 4: Add migration block in `_migrate`**

In `claw_v2/memory.py`, at the end of `_migrate` (before line 315 `try: self.backfill_outcome_embeddings()`), insert:

```python
        cursor = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='outcome_entity_edges'"
        )
        if cursor.fetchone() is None:
            try:
                self._conn.executescript(
                    "CREATE TABLE IF NOT EXISTS outcome_entity_edges ("
                    "outcome_id INTEGER NOT NULL REFERENCES task_outcomes(id) ON DELETE CASCADE, "
                    "entity_tag TEXT NOT NULL, "
                    "PRIMARY KEY (outcome_id, entity_tag)); "
                    "CREATE INDEX IF NOT EXISTS idx_outcome_entity_tag "
                    "ON outcome_entity_edges(entity_tag);"
                )
                self._conn.commit()
            except sqlite3.OperationalError:
                pass
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_memory_core.py::EntityEdgesSchemaTests -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/test_memory_core.py claw_v2/memory.py
git commit -m "feat(memory): outcome_entity_edges schema + migration (Task 4/10 GraphRAG)"
```

---

### Task 5: Edge population on outcome write

**Files:**
- Modify: `claw_v2/memory.py` (`store_task_outcome` at line 959, `store_task_outcome_with_embedding` at line 983)
- Test: `tests/test_memory_core.py` (new `EdgePopulationTests` class)

**Rationale:** Every outcome write must create one row in `outcome_entity_edges` per tag. The `tags` column on `task_outcomes` is a JSON array (default `'[]'` per migration at line 186); we parse it and insert. Both write methods need the same logic — extract a private helper `_index_outcome_tags`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_core.py — append
class EdgePopulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore(Path(tempfile.mkdtemp()) / "test.db")

    def _edges_for(self, outcome_id: int) -> set[str]:
        rows = self.store._conn.execute(
            "SELECT entity_tag FROM outcome_entity_edges WHERE outcome_id = ?",
            (outcome_id,),
        ).fetchall()
        return {r[0] for r in rows}

    def test_store_outcome_with_tags_creates_edges(self) -> None:
        oid = self.store.store_task_outcome(
            task_type="browse", task_id="s1:1",
            description="d", approach="a", outcome="success", lesson="l",
            tags=["tradingview", "cdp"],
        )
        self.assertEqual(self._edges_for(oid), {"tradingview", "cdp"})

    def test_store_outcome_without_tags_creates_no_edges(self) -> None:
        oid = self.store.store_task_outcome(
            task_type="browse", task_id="s1:1",
            description="d", approach="a", outcome="success", lesson="l",
        )
        self.assertEqual(self._edges_for(oid), set())

    def test_store_outcome_with_embedding_also_indexes_tags(self) -> None:
        oid = self.store.store_task_outcome_with_embedding(
            task_type="browse", task_id="s1:2",
            description="d", approach="a", outcome="failure", lesson="l",
            tags=["firecrawl"],
        )
        self.assertEqual(self._edges_for(oid), {"firecrawl"})

    def test_duplicate_tags_ignored(self) -> None:
        oid = self.store.store_task_outcome(
            task_type="browse", task_id="s1:3",
            description="d", approach="a", outcome="success", lesson="l",
            tags=["x", "x", "y"],
        )
        self.assertEqual(self._edges_for(oid), {"x", "y"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_memory_core.py::EdgePopulationTests -v`
Expected: All 4 fail. `store_task_outcome` and `store_task_outcome_with_embedding` don't accept `tags` parameter today.

- [ ] **Step 3: Add `tags` parameter to both write methods + edge indexing helper**

In `claw_v2/memory.py`, immediately before `store_task_outcome` (line 959), add the helper:

```python
    def _index_outcome_tags(self, outcome_id: int, tags: Iterable[str]) -> None:
        """Insert (outcome_id, tag) rows into outcome_entity_edges. Caller holds self._lock."""
        seen: set[str] = set()
        for tag in tags:
            t = str(tag).strip().lower()
            if not t or t in seen:
                continue
            seen.add(t)
            self._conn.execute(
                "INSERT OR IGNORE INTO outcome_entity_edges (outcome_id, entity_tag) "
                "VALUES (?, ?)",
                (outcome_id, t),
            )
```

Update `store_task_outcome` signature and body (line 959-981). Replace the existing method with:

```python
    def store_task_outcome(
        self,
        *,
        task_type: str,
        task_id: str,
        description: str,
        approach: str,
        outcome: str,
        lesson: str,
        error_snippet: str | None = None,
        retries: int = 0,
        tags: Iterable[str] = (),
    ) -> int:
        tag_list = list(tags)
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO task_outcomes
                    (task_type, task_id, description, approach, outcome, lesson,
                     error_snippet, retries, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_type, task_id, description, approach, outcome, lesson,
                 error_snippet, retries, json.dumps(tag_list)),
            )
            oid = cursor.lastrowid
            if tag_list:
                self._index_outcome_tags(oid, tag_list)
            self._conn.commit()
        return oid  # type: ignore[return-value]
```

Update `store_task_outcome_with_embedding` similarly (line 983-1016). Replace its body with:

```python
    def store_task_outcome_with_embedding(
        self,
        *,
        task_type: str,
        task_id: str,
        description: str,
        approach: str,
        outcome: str,
        lesson: str,
        error_snippet: str | None = None,
        retries: int = 0,
        tags: Iterable[str] = (),
        embed_fn: Callable[..., list[float]] | None = None,
    ) -> int:
        embedder = embed_fn or _simple_embedding
        tag_list = list(tags)
        with self._lock:
            text = f"{description} | {approach} | {lesson}"
            if error_snippet:
                text += f" | {error_snippet}"
            embedding = embedder(text)
            cursor = self._conn.execute(
                """
                INSERT INTO task_outcomes
                    (task_type, task_id, description, approach, outcome, lesson,
                     error_snippet, retries, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_type, task_id, description, approach, outcome, lesson,
                 error_snippet, retries, json.dumps(tag_list)),
            )
            oid = cursor.lastrowid
            self._conn.execute(
                "INSERT INTO outcome_embeddings (outcome_id, embedding) VALUES (?, ?)",
                (oid, json.dumps(embedding)),
            )
            if tag_list:
                self._index_outcome_tags(oid, tag_list)
            self._conn.commit()
        return oid  # type: ignore[return-value]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_memory_core.py::EdgePopulationTests tests/test_brain_core.py -v`
Expected: 4 new tests pass; `test_brain_core` still green (the new `tags` parameter has a default of `()`, so all callers stay compatible).

- [ ] **Step 5: Commit**

```bash
git add tests/test_memory_core.py claw_v2/memory.py
git commit -m "feat(memory): index outcome tags into entity-edge table on write (Task 5/10 GraphRAG)"
```

---

### Task 6: Backfill existing outcome edges

**Files:**
- Modify: `claw_v2/memory.py` (add `backfill_outcome_entity_edges` method; call from `_migrate` after the new table is ensured)
- Test: `tests/test_memory_core.py` (new `EdgeBackfillTests` class)

**Rationale:** Existing rows in `task_outcomes` have populated `tags` JSON but no rows in the new `outcome_entity_edges` table. Lazy backfill on `MemoryStore.__init__` mirrors the existing `backfill_outcome_embeddings` pattern (line 935-955).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_core.py — append
class EdgeBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = Path(tempfile.mkdtemp()) / "test.db"
        self.store = MemoryStore(self.db_path)

    def test_backfill_indexes_pre_existing_outcomes(self) -> None:
        # Insert an outcome the "old way" — directly with tags JSON, bypassing the new edge index.
        self.store._conn.execute(
            "INSERT INTO task_outcomes (task_type, task_id, description, approach, "
            "outcome, lesson, tags) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("browse", "s1:1", "d", "a", "success", "l", '["foo", "bar"]'),
        )
        self.store._conn.commit()
        # Confirm no edges exist yet.
        rows_before = self.store._conn.execute(
            "SELECT COUNT(*) FROM outcome_entity_edges"
        ).fetchone()
        self.assertEqual(rows_before[0], 0)

        count = self.store.backfill_outcome_entity_edges()
        self.assertEqual(count, 1)

        rows_after = self.store._conn.execute(
            "SELECT entity_tag FROM outcome_entity_edges"
        ).fetchall()
        self.assertEqual({r[0] for r in rows_after}, {"foo", "bar"})

    def test_backfill_invoked_on_store_init(self) -> None:
        # Insert via direct SQL to simulate legacy rows.
        self.store._conn.execute(
            "INSERT INTO task_outcomes (task_type, task_id, description, approach, "
            "outcome, lesson, tags) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("browse", "s1:1", "d", "a", "success", "l", '["legacy"]'),
        )
        self.store._conn.commit()
        # Re-open the DB — the new MemoryStore should backfill on init.
        store2 = MemoryStore(self.db_path)
        rows = store2._conn.execute(
            "SELECT entity_tag FROM outcome_entity_edges"
        ).fetchall()
        self.assertEqual({r[0] for r in rows}, {"legacy"})

    def test_backfill_skips_already_indexed(self) -> None:
        # Insert with new write path (auto-indexes).
        self.store.store_task_outcome(
            task_type="browse", task_id="s1:1",
            description="d", approach="a", outcome="success", lesson="l",
            tags=["alpha"],
        )
        # Backfill should report 0 — nothing left to index.
        count = self.store.backfill_outcome_entity_edges()
        self.assertEqual(count, 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_memory_core.py::EdgeBackfillTests -v`
Expected: All 3 fail. `backfill_outcome_entity_edges` does not exist; `_migrate` does not call it.

- [ ] **Step 3: Implement `backfill_outcome_entity_edges` and wire into `_migrate`**

In `claw_v2/memory.py`, immediately after `backfill_outcome_embeddings` (line 935-955), add:

```python
    def backfill_outcome_entity_edges(self) -> int:
        """Populate outcome_entity_edges from task_outcomes.tags JSON for any rows
        that have tags but no edges. Returns count of outcomes backfilled.
        """
        rows = self._conn.execute(
            "SELECT id, tags FROM task_outcomes "
            "WHERE id NOT IN (SELECT DISTINCT outcome_id FROM outcome_entity_edges) "
            "AND tags IS NOT NULL AND tags != '[]'"
        ).fetchall()
        backfilled = 0
        with self._lock:
            for row in rows:
                try:
                    tags = json.loads(row["tags"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if not tags:
                    continue
                self._index_outcome_tags(row["id"], tags)
                backfilled += 1
            self._conn.commit()
        return backfilled
```

In `_migrate`, find the existing block at line 315-318:

```python
        try:
            self.backfill_outcome_embeddings()
        except Exception:
            logger.debug("Outcome embedding backfill skipped", exc_info=True)
```

Append immediately after it:

```python
        try:
            self.backfill_outcome_entity_edges()
        except Exception:
            logger.debug("Outcome entity edges backfill skipped", exc_info=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_memory_core.py::EdgeBackfillTests -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_memory_core.py claw_v2/memory.py
git commit -m "feat(memory): backfill outcome entity edges on init (Task 6/10 GraphRAG)"
```

---

### Task 7: Graph neighbors helper

**Files:**
- Modify: `claw_v2/memory.py` (add `_outcome_graph_neighbors` method)
- Test: `tests/test_memory_core.py` (new `OutcomeGraphNeighborsTests` class)

**Rationale:** Given a seed outcome id, return outcome ids that share at least one entity tag, optionally up to depth N. Mirrors `wiki.py:_graph_neighbors` (line 1388) but uses SQL joins instead of an in-memory dict because edges live in SQLite. Depth=1 is sufficient for the MVP.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_core.py — append
class OutcomeGraphNeighborsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore(Path(tempfile.mkdtemp()) / "test.db")

    def test_neighbors_share_at_least_one_tag(self) -> None:
        a = self.store.store_task_outcome(
            task_type="browse", task_id="s1:1", description="d", approach="a",
            outcome="success", lesson="l", tags=["tradingview", "cdp"],
        )
        b = self.store.store_task_outcome(
            task_type="browse", task_id="s1:2", description="d", approach="a",
            outcome="failure", lesson="l", tags=["tradingview"],
        )
        c = self.store.store_task_outcome(
            task_type="browse", task_id="s1:3", description="d", approach="a",
            outcome="success", lesson="l", tags=["unrelated"],
        )
        neighbors = self.store._outcome_graph_neighbors([a])
        self.assertIn(b, neighbors)
        self.assertNotIn(c, neighbors)
        self.assertNotIn(a, neighbors)  # exclude seeds themselves

    def test_no_neighbors_when_no_tags(self) -> None:
        a = self.store.store_task_outcome(
            task_type="browse", task_id="s1:1", description="d", approach="a",
            outcome="success", lesson="l",
        )
        self.assertEqual(self.store._outcome_graph_neighbors([a]), [])

    def test_multiple_seeds_combined(self) -> None:
        a = self.store.store_task_outcome(
            task_type="t", task_id="i:1", description="d", approach="a",
            outcome="success", lesson="l", tags=["alpha"],
        )
        b = self.store.store_task_outcome(
            task_type="t", task_id="i:2", description="d", approach="a",
            outcome="failure", lesson="l", tags=["beta"],
        )
        n_a = self.store.store_task_outcome(
            task_type="t", task_id="i:3", description="d", approach="a",
            outcome="success", lesson="l", tags=["alpha"],
        )
        n_b = self.store.store_task_outcome(
            task_type="t", task_id="i:4", description="d", approach="a",
            outcome="failure", lesson="l", tags=["beta"],
        )
        neighbors = set(self.store._outcome_graph_neighbors([a, b]))
        self.assertEqual(neighbors, {n_a, n_b})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_memory_core.py::OutcomeGraphNeighborsTests -v`
Expected: 3 failures, all `AttributeError: 'MemoryStore' object has no attribute '_outcome_graph_neighbors'`.

- [ ] **Step 3: Add the helper**

In `claw_v2/memory.py`, immediately before `search_outcomes_semantic` (around line 1091, but after `backfill_outcome_entity_edges`), add:

```python
    def _outcome_graph_neighbors(self, seed_ids: list[int]) -> list[int]:
        """Return outcome ids that share at least one entity tag with any seed.

        Excludes the seed ids themselves. Single-hop only (depth=1). Mirrors the pattern
        from claw_v2/wiki.py:_graph_neighbors but operates over a SQL edge table.
        """
        if not seed_ids:
            return []
        placeholders = ",".join("?" for _ in seed_ids)
        rows = self._conn.execute(
            f"""
            SELECT DISTINCT e2.outcome_id
            FROM outcome_entity_edges e1
            JOIN outcome_entity_edges e2 ON e1.entity_tag = e2.entity_tag
            WHERE e1.outcome_id IN ({placeholders})
              AND e2.outcome_id NOT IN ({placeholders})
            """,
            (*seed_ids, *seed_ids),
        ).fetchall()
        return [row[0] for row in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_memory_core.py::OutcomeGraphNeighborsTests -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_memory_core.py claw_v2/memory.py
git commit -m "feat(memory): _outcome_graph_neighbors via shared entity tags (Task 7/10 GraphRAG)"
```

---

### Task 8: `search_outcomes_with_graph` (hybrid + graph expansion)

**Files:**
- Modify: `claw_v2/memory.py` (add `search_outcomes_with_graph` method)
- Test: `tests/test_memory_core.py` (new `SearchOutcomesWithGraphTests` class)

**Rationale:** Combines Tasks 3 + 7. Run hybrid `search_outcomes_semantic`, take top-K seeds, fetch their neighbors, score neighbors at 0.6× the average seed score (mirrors `wiki.py:1046`), merge, deduplicate. Returns the same dict shape as `search_outcomes_semantic`, with an extra `via_graph: bool` field per result.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_core.py — append
class SearchOutcomesWithGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore(Path(tempfile.mkdtemp()) / "test.db")

    def test_graph_expansion_surfaces_unrelated_text_neighbor(self) -> None:
        # Direct hit: the query "tradingview" matches this lesson textually.
        seed = self.store.store_task_outcome_with_embedding(
            task_type="browse", task_id="s1:1",
            description="Tradingview chart capture", approach="cdp",
            outcome="failure", lesson="cdp needs user-data-dir",
            tags=["tradingview", "cdp"],
        )
        # Graph-only hit: shares tag "cdp" with seed but mentions nothing about tradingview.
        neighbor = self.store.store_task_outcome_with_embedding(
            task_type="browse", task_id="s1:2",
            description="Generic page failed", approach="default browser",
            outcome="failure", lesson="Pages with auth need persistent profile",
            tags=["cdp", "auth"],
        )
        # Unrelated outcome — should not appear.
        self.store.store_task_outcome_with_embedding(
            task_type="browse", task_id="s1:3",
            description="Random other failure", approach="x",
            outcome="failure", lesson="totally unrelated",
            tags=["other"],
        )

        plain = {r["task_id"] for r in self.store.search_outcomes_semantic("tradingview", limit=5)}
        with_graph = {r["task_id"] for r in self.store.search_outcomes_with_graph("tradingview", limit=5)}
        self.assertIn("s1:1", plain)
        self.assertNotIn("s1:2", plain)  # plain semantic+BM25 misses it
        self.assertIn("s1:1", with_graph)
        self.assertIn("s1:2", with_graph)  # graph surfaces it

    def test_graph_results_marked_via_graph(self) -> None:
        seed = self.store.store_task_outcome_with_embedding(
            task_type="t", task_id="i:1",
            description="Tradingview snapshot", approach="a", outcome="success", lesson="l",
            tags=["tradingview"],
        )
        neighbor = self.store.store_task_outcome_with_embedding(
            task_type="t", task_id="i:2",
            description="Other thing entirely", approach="a", outcome="failure", lesson="l",
            tags=["tradingview"],
        )
        results = self.store.search_outcomes_with_graph("tradingview", limit=5)
        by_task = {r["task_id"]: r for r in results}
        self.assertFalse(by_task["i:1"]["via_graph"])
        self.assertTrue(by_task["i:2"]["via_graph"])

    def test_graph_score_below_seed_score(self) -> None:
        seed = self.store.store_task_outcome_with_embedding(
            task_type="t", task_id="i:1",
            description="firecrawl tradingview", approach="a", outcome="success", lesson="l",
            tags=["alpha"],
        )
        self.store.store_task_outcome_with_embedding(
            task_type="t", task_id="i:2",
            description="unrelated text content", approach="a", outcome="failure", lesson="l",
            tags=["alpha"],
        )
        results = self.store.search_outcomes_with_graph("firecrawl tradingview", limit=5)
        by_task = {r["task_id"]: r for r in results}
        self.assertGreater(by_task["i:1"]["score"], by_task["i:2"]["score"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_memory_core.py::SearchOutcomesWithGraphTests -v`
Expected: 3 failures — `search_outcomes_with_graph` does not exist.

- [ ] **Step 3: Implement the method**

In `claw_v2/memory.py`, immediately after `search_outcomes_semantic`, add:

```python
    def search_outcomes_with_graph(
        self,
        query: str,
        *,
        task_type: str | None = None,
        limit: int = 5,
        seed_k: int = 3,
        min_similarity: float = 0.1,
        embed_fn: Callable[..., list[float]] | None = None,
    ) -> list[dict]:
        """Hybrid retrieval (BM25+cosine) plus graph expansion via shared entity tags.

        Steps:
          1. Run search_outcomes_semantic to get hybrid-scored seeds.
          2. Take top `seed_k` seeds, look up their entity-tag neighbors.
          3. Fetch neighbor outcome rows in full, score each at 0.6 * avg(seed.score).
          4. Merge, dedupe (seeds win on ties), sort by score descending.

        Each result dict includes via_graph: bool indicating whether it came from
        graph expansion (True) or direct hybrid match (False). Mirrors the
        seed-then-expand pattern at claw_v2/wiki.py:1032-1050.
        """
        seeds = self.search_outcomes_semantic(
            query, task_type=task_type, limit=max(limit, seed_k * 2),
            min_similarity=min_similarity, embed_fn=embed_fn,
        )
        for seed in seeds:
            seed["via_graph"] = False

        seed_ids = [s["id"] for s in seeds[:seed_k] if s.get("id") is not None]
        if not seed_ids:
            return seeds[:limit]

        neighbor_ids = self._outcome_graph_neighbors(seed_ids)
        seed_id_set = {s["id"] for s in seeds if s.get("id") is not None}
        neighbor_ids = [nid for nid in neighbor_ids if nid not in seed_id_set]
        if not neighbor_ids:
            return sorted(seeds, key=lambda r: r["score"], reverse=True)[:limit]

        avg_seed_score = sum(s["score"] for s in seeds[:seed_k]) / max(len(seeds[:seed_k]), 1)
        graph_score = round(avg_seed_score * 0.6, 4)

        placeholders = ",".join("?" for _ in neighbor_ids)
        rows = self._conn.execute(
            f"SELECT id, task_type, task_id, description, approach, outcome, lesson, "
            f"error_snippet, retries, created_at, feedback "
            f"FROM task_outcomes WHERE id IN ({placeholders})",
            neighbor_ids,
        ).fetchall()
        for row in rows:
            item = dict(row)
            item["similarity"] = 0.0
            item["keyword_score"] = 0.0
            item["score"] = graph_score
            item["via_graph"] = True
            seeds.append(item)

        seeds.sort(key=lambda r: r["score"], reverse=True)
        return seeds[:limit]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_memory_core.py::SearchOutcomesWithGraphTests -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_memory_core.py claw_v2/memory.py
git commit -m "feat(memory): search_outcomes_with_graph (hybrid + entity-tag expansion) (Task 8/10 GraphRAG)"
```

---

### Task 9: `LearningLoop.retrieve_lessons` uses graph search

**Files:**
- Modify: `claw_v2/learning.py` (`retrieve_lessons` at line 90-159)
- Test: `tests/test_memory_scoped.py` (new `RetrieveLessonsViaGraphTests` class)

**Rationale:** Make graph expansion the default retrieval path for the prompt-injected lessons. Keep the existing fallback chain (semantic → LIKE → recent failures) unchanged below the new top-of-funnel call. Add a `via_graph` indicator in the rendered XML so the brain knows when a lesson came from neighborhood expansion.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_scoped.py — append
class RetrieveLessonsViaGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore(Path(tempfile.mkdtemp()) / "test.db")
        self.loop = LearningLoop(memory=self.store)

    def test_retrieves_graph_neighbor_lesson(self) -> None:
        # Seed match by text: "firecrawl"
        self.store.store_task_outcome_with_embedding(
            task_type="browse", task_id="s1:1",
            description="firecrawl scrape failed",
            approach="firecrawl scrape https://x", outcome="failure",
            lesson="firecrawl needs api key in env",
            tags=["firecrawl", "scrape"],
        )
        # Graph-only neighbor: shares tag "scrape" but doesn't mention firecrawl.
        self.store.store_task_outcome_with_embedding(
            task_type="browse", task_id="s1:2",
            description="page extraction stalled",
            approach="default scraper", outcome="failure",
            lesson="set explicit timeout for SPA pages",
            tags=["scrape", "spa"],
        )
        rendered = self.loop.retrieve_lessons("firecrawl scrape attempt")
        self.assertIn("firecrawl needs api key", rendered)
        self.assertIn("set explicit timeout for SPA pages", rendered)
        self.assertIn("via_graph=\"true\"", rendered)

    def test_falls_back_to_semantic_when_no_graph_results(self) -> None:
        # Only seeds, no neighbors — old behavior should still produce content.
        self.store.store_task_outcome_with_embedding(
            task_type="browse", task_id="s1:1",
            description="firecrawl scrape", approach="a",
            outcome="failure", lesson="api key required",
        )
        rendered = self.loop.retrieve_lessons("firecrawl")
        self.assertIn("api key required", rendered)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_memory_scoped.py::RetrieveLessonsViaGraphTests -v`
Expected: `test_retrieves_graph_neighbor_lesson` fails — current `retrieve_lessons` doesn't expand via graph and won't find the SPA-timeout lesson. The fallback test may pass.

- [ ] **Step 3: Modify `retrieve_lessons`**

In `claw_v2/learning.py`, replace the `outcomes: list[dict] = []` block at lines 109-116 with:

```python
        outcomes: list[dict] = []
        try:
            outcomes = self.memory.search_outcomes_with_graph(
                keywords, task_type=task_type, limit=limit, embed_fn=embed_fn,
            )
        except Exception:
            logger.debug("Graph outcome search failed, falling back to semantic", exc_info=True)

        if not outcomes:
            try:
                outcomes = self.memory.search_outcomes_semantic(
                    keywords, task_type=task_type, limit=limit, embed_fn=embed_fn,
                )
            except Exception:
                logger.debug("Semantic outcome search failed, falling back to text search", exc_info=True)
```

Also update the rendering loop (lines 144-158). Find the line:

```python
            sim_attr = f' similarity="{sim}"' if sim is not None else ""
            out_lines.append(f'<learned_lesson status="{status}"{sim_attr}>')
```

Replace with:

```python
            sim_attr = f' similarity="{sim}"' if sim is not None else ""
            via_graph = o.get("via_graph", False)
            graph_attr = ' via_graph="true"' if via_graph else ""
            out_lines.append(f'<learned_lesson status="{status}"{sim_attr}{graph_attr}>')
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_memory_scoped.py::RetrieveLessonsViaGraphTests tests/test_brain_core.py tests/test_brain_verify.py -v`
Expected: new tests pass; brain tests still green.

- [ ] **Step 5: Commit**

```bash
git add tests/test_memory_scoped.py claw_v2/learning.py
git commit -m "feat(learning): retrieve_lessons uses graph-expanded outcome search (Task 9/10 GraphRAG)"
```

---

### Task 10: ObserveStream signal + full regression

**Files:**
- Modify: `claw_v2/learning.py` (add observe emit when graph contributes results)
- Test: `tests/test_memory_scoped.py` (new `RetrieveLessonsObserveSignalTests` class)

**Rationale:** Make the new graph contribution observable so it shows up in the trace and we can quantify how often graph expansion adds value. Inject the optional `ObserveStream` via the `LearningLoop` dataclass (it already imports `ObserveStream` under `TYPE_CHECKING` at line 14).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_scoped.py — append
class RetrieveLessonsObserveSignalTests(unittest.TestCase):
    def setUp(self) -> None:
        from claw_v2.observe import ObserveStream
        self.store = MemoryStore(Path(tempfile.mkdtemp()) / "test.db")
        self.observe = ObserveStream(Path(tempfile.mkdtemp()) / "observe.db")
        self.loop = LearningLoop(memory=self.store, observe=self.observe)

    def test_emits_event_when_graph_contributes(self) -> None:
        self.store.store_task_outcome_with_embedding(
            task_type="browse", task_id="s1:1",
            description="firecrawl call", approach="a",
            outcome="failure", lesson="key required", tags=["firecrawl"],
        )
        self.store.store_task_outcome_with_embedding(
            task_type="browse", task_id="s1:2",
            description="another thing entirely", approach="a",
            outcome="failure", lesson="set timeout", tags=["firecrawl"],
        )
        self.loop.retrieve_lessons("firecrawl")
        events = self.observe.recent_events(limit=10)
        kinds = [e["event_type"] for e in events]
        self.assertIn("lessons_graph_hit", kinds)

    def test_no_event_when_only_seeds(self) -> None:
        self.store.store_task_outcome_with_embedding(
            task_type="browse", task_id="s1:1",
            description="solo", approach="a",
            outcome="success", lesson="ok",
        )
        self.loop.retrieve_lessons("solo")
        events = self.observe.recent_events(limit=10)
        kinds = [e["event_type"] for e in events]
        self.assertNotIn("lessons_graph_hit", kinds)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_memory_scoped.py::RetrieveLessonsObserveSignalTests -v`
Expected: 2 failures — `LearningLoop.__init__` does not accept `observe` parameter; no event is emitted.

- [ ] **Step 3: Add `observe` field and emit logic**

In `claw_v2/learning.py`, modify the dataclass (lines 19-23):

```python
@dataclass(slots=True)
class LearningLoop:
    memory: MemoryStore
    router: LLMRouter | None = None
    observe: ObserveStream | None = None
    _last_outcome_id: int | None = field(default=None, repr=False)
```

Then, inside `retrieve_lessons`, after the graph search call but before the fallback chain (right after the `try / except` block from Task 9), add:

```python
        if outcomes and self.observe is not None:
            graph_count = sum(1 for o in outcomes if o.get("via_graph"))
            if graph_count:
                try:
                    self.observe.emit(
                        "lessons_graph_hit",
                        payload={
                            "graph_count": graph_count,
                            "total": len(outcomes),
                            "task_type": task_type or "any",
                        },
                    )
                except Exception:
                    logger.debug("Observe emit for lessons_graph_hit failed", exc_info=True)
```

Update the import block at lines 11-14 to also import `ObserveStream` at runtime (it currently only imports under `TYPE_CHECKING`):

Change:

```python
if TYPE_CHECKING:
    from claw_v2.llm import LLMRouter
    from claw_v2.memory import MemoryStore
    from claw_v2.observe import ObserveStream
```

to:

```python
from claw_v2.observe import ObserveStream

if TYPE_CHECKING:
    from claw_v2.llm import LLMRouter
    from claw_v2.memory import MemoryStore
```

(`ObserveStream` is needed at runtime now because `dataclass(slots=True)` needs the actual type for the field annotation.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_memory_scoped.py::RetrieveLessonsObserveSignalTests -v`
Expected: 2 passed.

- [ ] **Step 5: Run full test suite to verify no regressions**

Run: `python -m pytest tests/ -x --ignore=tests/test_wiki.py 2>&1 | tail -40`
Expected: all pass except any pre-existing wiki test failures noted in the session-start banner (those 30 failing wiki tests are unrelated to this work — they were failing before this plan started).

If a previously-green test now fails, stop and investigate before committing. The most likely failure points are:
- `tests/test_brain_core.py` — `LearningLoop` is constructed there; if a positional argument is passed, the new `observe` field may shift positions. (Mitigated by `kw_only` discipline; verify call sites use keyword args.)
- `tests/test_dream.py` — uses outcomes; should be unaffected.
- `tests/test_kairos.py` — uses observe + memory; unaffected.

- [ ] **Step 6: Commit**

```bash
git add tests/test_memory_scoped.py claw_v2/learning.py
git commit -m "feat(learning): emit lessons_graph_hit observe event (Task 10/10 GraphRAG)"
```

---

## Self-Review Checklist (executed by plan author)

**1. Spec coverage:**
- ✓ Hybrid retrieval (BM25 + cosine) for facts → Task 2.
- ✓ Hybrid retrieval for outcomes → Task 3.
- ✓ Entity graph storage → Tasks 4–6.
- ✓ Graph traversal → Task 7.
- ✓ Graph-expanded retrieval → Task 8.
- ✓ Production wiring (LearningLoop) → Task 9.
- ✓ Observability → Task 10.
- ✓ No facts-graph (explicit non-goal — profile facts don't benefit from co-tag expansion).

**2. Placeholder scan:** No `TBD`, `TODO`, `add appropriate error handling`, `similar to Task N` references. Every step shows the exact code or command.

**3. Type consistency:**
- `_outcome_graph_neighbors(seed_ids: list[int]) -> list[int]` — used consistently in Task 7 (definition) and Task 8 (call site).
- `search_outcomes_with_graph(...) -> list[dict]` returns dicts with keys `id, task_type, task_id, description, approach, outcome, lesson, error_snippet, retries, created_at, feedback, similarity, keyword_score, score, via_graph` — Task 8 produces them; Task 9 reads `o.get("via_graph", False)`; Task 10 reads `o.get("via_graph")`. Consistent.
- `_index_outcome_tags(outcome_id: int, tags: Iterable[str]) -> None` — defined Task 5, called from same file in Task 5 (writes) and Task 6 (backfill). Consistent.
- `tags` parameter on `store_task_outcome` and `store_task_outcome_with_embedding` — `Iterable[str] = ()` in both, consistent.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-19-graphrag-memory.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
