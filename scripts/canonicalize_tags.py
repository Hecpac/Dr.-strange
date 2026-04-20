"""Canonicalize operational tags in outcome_entity_edges via LLM consolidator.

Extracts the top-N most frequent tags, asks the judge LLM to propose a
synonym -> canonical mapping, then rewrites outcome_entity_edges (and the
JSON tags column on task_outcomes) in-place. Outcomes whose canonical form
collides with another tag on the same outcome are deduped via INSERT OR
IGNORE (the edges table PK enforces it).

Idempotent: re-running after canonicalization is a no-op if the LLM
returns an empty mapping.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys

import anthropic

from claw_v2.config import AppConfig
from claw_v2.learning import _normalize_tags, _parse_json_object
from claw_v2.memory import MemoryStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("canonicalize_tags")

TOP_N = 300
MAX_TAGS_PER_PROMPT_CHUNK = 150


def fetch_top_tags(conn: sqlite3.Connection, n: int) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT entity_tag, COUNT(*) c "
        "FROM outcome_entity_edges "
        "GROUP BY entity_tag ORDER BY c DESC LIMIT ?",
        (n,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _anthropic_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-a", os.environ["USER"],
             "-s", "ANTHROPIC_API_KEY", "-w"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except Exception as exc:
        raise RuntimeError("ANTHROPIC_API_KEY not in env or Keychain") from exc


def ask_canonicalization(
    client: anthropic.Anthropic, tag_chunk: list[tuple[str, int]],
) -> dict[str, str]:
    """Return {synonym: canonical}. Skips identity mappings and no-ops."""
    tag_list_text = "\n".join(f"- {tag} ({count})" for tag, count in tag_chunk)
    prompt = (
        "You are consolidating an operational tag taxonomy for an AI agent's "
        "outcome graph. Tags are snake_case technical concepts naming root "
        "causes or affected components in the Claw codebase.\n\n"
        "TASK: Group near-synonyms into a canonical name. Only merge tags "
        "that mean the same operational concept. DO NOT merge tags that are "
        "operationally distinct (e.g. 'openai_rate_limit' and "
        "'anthropic_rate_limit' are different — they affect different "
        "providers; keep separate).\n\n"
        "RULES:\n"
        "- Prefer the shortest, most neutral name as canonical.\n"
        "- Leave tags alone if they have no clear synonym in the list.\n"
        "- Do NOT invent new tags; canonical must be drawn from the input.\n"
        "- Output must be snake_case.\n\n"
        "Return JSON ONLY in this shape: "
        '{"mapping": {"synonym_tag": "canonical_tag", ...}}\n\n'
        "Tags (with outcome count):\n"
        f"{tag_list_text}"
    )
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    content = "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    )
    parsed = _parse_json_object(content)
    if not parsed:
        logger.warning("LLM returned unparseable JSON; skipping chunk")
        return {}
    raw = parsed.get("mapping") or {}
    if not isinstance(raw, dict):
        return {}
    valid_tags = {t for t, _ in tag_chunk}
    mapping: dict[str, str] = {}
    for syn, canon in raw.items():
        if not isinstance(syn, str) or not isinstance(canon, str):
            continue
        syn_norm = _normalize_tags([syn])
        canon_norm = _normalize_tags([canon])
        if not syn_norm or not canon_norm:
            continue
        s, c = syn_norm[0], canon_norm[0]
        if s == c:
            continue
        if c not in valid_tags:
            continue
        mapping[s] = c
    return mapping


def resolve_chain(mapping: dict[str, str]) -> dict[str, str]:
    """Collapse transitive chains (a→b, b→c ⇒ a→c) and drop cycles."""
    resolved: dict[str, str] = {}
    for syn in mapping:
        seen = {syn}
        cur = syn
        while cur in mapping and mapping[cur] != cur:
            nxt = mapping[cur]
            if nxt in seen:
                break
            seen.add(nxt)
            cur = nxt
        if cur != syn:
            resolved[syn] = cur
    return resolved


def apply_mapping(memory: MemoryStore, mapping: dict[str, str]) -> tuple[int, int]:
    """Rewrite outcome_entity_edges and task_outcomes.tags.

    Returns (edges_rewritten, outcomes_touched).
    """
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
                canon = mapping.get(t, t)
                if canon in seen:
                    continue
                seen.add(canon)
                new_tags.append(canon)
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
    client = anthropic.Anthropic(api_key=_anthropic_api_key())

    tags = fetch_top_tags(memory._conn, TOP_N)
    if not tags:
        logger.info("No tags in graph. Nothing to consolidate.")
        return 0

    logger.info("Consolidating top %d tags (coverage: %d edges)",
                len(tags), sum(c for _, c in tags))

    full_mapping: dict[str, str] = {}
    for i in range(0, len(tags), MAX_TAGS_PER_PROMPT_CHUNK):
        chunk = tags[i:i + MAX_TAGS_PER_PROMPT_CHUNK]
        logger.info("Asking LLM for mapping on chunk %d (%d tags)",
                    i // MAX_TAGS_PER_PROMPT_CHUNK + 1, len(chunk))
        chunk_mapping = ask_canonicalization(client, chunk)
        logger.info("  chunk proposed %d synonyms", len(chunk_mapping))
        full_mapping.update(chunk_mapping)

    full_mapping = resolve_chain(full_mapping)
    if not full_mapping:
        logger.info("LLM proposed no consolidations. Graph is already canonical.")
        return 0

    logger.info("Applying %d synonym->canonical mappings...", len(full_mapping))
    preview = list(full_mapping.items())[:15]
    for syn, canon in preview:
        logger.info("  %s  ->  %s", syn, canon)
    if len(full_mapping) > 15:
        logger.info("  ... and %d more", len(full_mapping) - 15)

    edges_rewritten, outcomes_touched = apply_mapping(memory, full_mapping)

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
        logger.info("Interrupted by user.")
        sys.exit(130)
