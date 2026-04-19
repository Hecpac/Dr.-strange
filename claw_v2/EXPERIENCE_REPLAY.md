# Experience Replay

Every call to `LearningLoop.record(...)` — and every Brain verification cycle — stores a
post-mortem in `task_outcomes` together with a sentence-embedding in `outcome_embeddings`.
`LearningLoop.retrieve_lessons(...)` is called from `BrainService._build_prompt` before
every LLM call and prefers semantic recall (vector cosine) over the legacy LIKE search,
with a fallback chain: semantic → LIKE → recent failures.

When a lesson is injected into a prompt, Brain emits the observe event
`experience_replay_retrieved` with a short preview. `BrainService._emit_verification_outcome(...)`
is the helper that emits `cycle_verification_complete` and auto-records the outcome via
`LearningLoop.record_cycle_outcome(...)`; callers invoke it at cycle boundaries.

Backfill of embeddings for legacy outcomes runs once at `MemoryStore` open time.
