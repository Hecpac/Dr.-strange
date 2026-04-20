"""Heuristic tag canonicalization via token-overlap clustering (no LLM).

Groups tags whose token sets have Jaccard similarity >= THRESHOLD. Within
each cluster, the canonical name is the tag with the highest outcome count
(ties broken by shortest name). Rewrites outcome_entity_edges and the JSON
tags column on task_outcomes.

Only operates on the top-N most frequent tags — the long tail stays as-is
(fusing rare tags yields noise, not signal).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from collections import defaultdict

from claw_v2.config import AppConfig
from claw_v2.memory import MemoryStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("canonicalize_heuristic")

TOP_N = 500
JACCARD_THRESHOLD = 0.65
MIN_SHARED_TOKENS = 2
MIN_TOKEN_LEN = 1


def fetch_top_tags(conn: sqlite3.Connection, n: int) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT entity_tag, COUNT(*) c FROM outcome_entity_edges "
        "GROUP BY entity_tag ORDER BY c DESC, entity_tag ASC LIMIT ?",
        (n,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def tokens_of(tag: str) -> frozenset[str]:
    return frozenset(t for t in tag.split("_") if len(t) >= MIN_TOKEN_LEN)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = a & b
    if len(inter) < MIN_SHARED_TOKENS:
        return 0.0
    return len(inter) / len(a | b)


def cluster(tags: list[tuple[str, int]]) -> list[list[tuple[str, int]]]:
    """Union-Find clustering. O(n^2) — fine for n<=500."""
    n = len(tags)
    parent = list(range(n))
    token_sets = [tokens_of(t) for t, _ in tags]

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if jaccard(token_sets[i], token_sets[j]) >= JACCARD_THRESHOLD:
                union(i, j)

    groups: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for i, item in enumerate(tags):
        groups[find(i)].append(item)
    return [g for g in groups.values() if len(g) > 1]


def pick_canonical(group: list[tuple[str, int]]) -> str:
    return sorted(group, key=lambda x: (-x[1], len(x[0]), x[0]))[0][0]


def build_mapping(groups: list[list[tuple[str, int]]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for group in groups:
        canon = pick_canonical(group)
        for tag, _ in group:
            if tag != canon:
                mapping[tag] = canon
    return mapping


def apply_mapping(memory: MemoryStore, mapping: dict[str, str]) -> tuple[int, int]:
    edges_rewritten = 0
    outcomes_touched = 0
    with memory._lock:
        conn = memory._conn
        affected_oids: set[int] = set()
        for syn, canon in mapping.items():
            rows = conn.execute(
                "SELECT outcome_id FROM outcome_entity_edges WHERE entity_tag = ?",
                (syn,),
            ).fetchall()
            for r in rows:
                oid = r[0]
                conn.execute(
                    "INSERT OR IGNORE INTO outcome_entity_edges "
                    "(outcome_id, entity_tag) VALUES (?, ?)",
                    (oid, canon),
                )
                conn.execute(
                    "DELETE FROM outcome_entity_edges "
                    "WHERE outcome_id = ? AND entity_tag = ?",
                    (oid, syn),
                )
                edges_rewritten += 1
                affected_oids.add(oid)

        for oid in affected_oids:
            row = conn.execute(
                "SELECT tags FROM task_outcomes WHERE id = ?", (oid,),
            ).fetchone()
            if not row or not row[0]:
                continue
            try:
                tags = json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(tags, list):
                continue
            new_tags: list[str] = []
            seen: set[str] = set()
            for t in tags:
                if not isinstance(t, str):
                    continue
                c = mapping.get(t, t)
                if c in seen:
                    continue
                seen.add(c)
                new_tags.append(c)
            if new_tags != tags:
                conn.execute(
                    "UPDATE task_outcomes SET tags = ? WHERE id = ?",
                    (json.dumps(new_tags), oid),
                )
                outcomes_touched += 1
        conn.commit()
    return edges_rewritten, outcomes_touched


def run() -> int:
    config = AppConfig.from_env()
    memory = MemoryStore(config.db_path)

    tags = fetch_top_tags(memory._conn, TOP_N)
    if not tags:
        logger.info("No tags in graph.")
        return 0

    logger.info("Clustering top %d tags (jaccard >= %.2f, min_shared=%d)...",
                len(tags), JACCARD_THRESHOLD, MIN_SHARED_TOKENS)
    groups = cluster(tags)
    mapping = build_mapping(groups)

    if not mapping:
        logger.info("No clusters found. Nothing to canonicalize.")
        return 0

    logger.info("Found %d clusters producing %d synonym mappings:",
                len(groups), len(mapping))
    for group in sorted(groups, key=lambda g: -sum(c for _, c in g))[:20]:
        canon = pick_canonical(group)
        members = ", ".join(t for t, _ in sorted(group, key=lambda x: -x[1]))
        logger.info("  %s  <=  [%s]", canon, members)
    if len(groups) > 20:
        logger.info("  ... and %d more clusters", len(groups) - 20)

    edges_rewritten, outcomes_touched = apply_mapping(memory, mapping)

    distinct_after = memory._conn.execute(
        "SELECT COUNT(DISTINCT entity_tag) FROM outcome_entity_edges"
    ).fetchone()[0]
    edges_after = memory._conn.execute(
        "SELECT COUNT(*) FROM outcome_entity_edges"
    ).fetchone()[0]
    logger.info("Done. rewrote %d edges across %d outcomes. "
                "graph now: %d edges, %d distinct tags.",
                edges_rewritten, outcomes_touched, edges_after, distinct_after)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except KeyboardInterrupt:
        logger.info("Interrupted.")
        sys.exit(130)
