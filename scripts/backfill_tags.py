"""Backfill operational tags for pre-existing task_outcomes.

Reuses LearningLoop._derive_outcome_metadata (same single-call LLM path used
in the live bot) and MemoryStore._index_outcome_tags (same edge indexer used
by store_task_outcome_with_embedding) — no duplicated logic.

Idempotent: only processes rows where tags is NULL or '[]'.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from claw_v2.adapters.anthropic import create_claude_sdk_executor
from claw_v2.config import AppConfig
from claw_v2.learning import LearningLoop
from claw_v2.llm import LLMRouter
from claw_v2.memory import MemoryStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("backfill_tags")


MAX_WORKERS = 8


def _process_one(
    learning: LearningLoop, memory: MemoryStore, row: dict,
) -> tuple[int, str]:
    """Derive tags via LLM and persist. Returns (outcome_id, status).

    status ∈ {"tagged", "empty", "error"}. Safe to call from multiple threads:
    LearningLoop._derive_outcome_metadata has no shared mutable state, and all
    DB writes go through memory._lock.
    """
    oid = row["id"]
    try:
        _lesson, tags = learning._derive_outcome_metadata(
            description=row["description"],
            approach=row["approach"],
            outcome=row["outcome"],
            error_snippet=row["error_snippet"],
        )
    except Exception as exc:
        logger.warning("outcome #%d: LLM derivation failed: %s", oid, exc)
        return oid, "error"

    if not tags:
        return oid, "empty"

    with memory._lock:
        memory._conn.execute(
            "UPDATE task_outcomes SET tags = ? WHERE id = ?",
            (json.dumps(tags), oid),
        )
        memory._index_outcome_tags(oid, tags)
        memory._conn.commit()
    return oid, "tagged"


def run() -> int:
    config = AppConfig.from_env()
    memory = MemoryStore(config.db_path)
    router = LLMRouter.default(
        config,
        anthropic_executor=create_claude_sdk_executor(config),
    )
    learning = LearningLoop(memory=memory, router=router)

    rows = memory._conn.execute(
        "SELECT id, description, approach, outcome, error_snippet "
        "FROM task_outcomes "
        "WHERE tags IS NULL OR tags = '[]' "
        "ORDER BY id ASC"
    ).fetchall()
    rows = [dict(r) for r in rows]
    total = len(rows)
    if total == 0:
        logger.info("No outcomes without tags. Nothing to do.")
        return 0

    logger.info("Backfilling tags for %d outcomes with %d workers...",
                total, MAX_WORKERS)

    tagged = 0
    skipped_empty = 0
    errors = 0
    done = 0
    progress_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_process_one, learning, memory, row) for row in rows]
        for fut in as_completed(futures):
            _oid, status = fut.result()
            with progress_lock:
                done += 1
                if status == "tagged":
                    tagged += 1
                elif status == "empty":
                    skipped_empty += 1
                else:
                    errors += 1
                if done % 25 == 0 or done == total:
                    logger.info("progress %d/%d — tagged=%d empty=%d errors=%d",
                                done, total, tagged, skipped_empty, errors)

    logger.info("Done. total=%d tagged=%d empty=%d errors=%d",
                total, tagged, skipped_empty, errors)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        sys.exit(130)
