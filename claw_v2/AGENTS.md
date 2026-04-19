# Agent Registry

Auto-updated every heartbeat.

| Agent | Model | Status | Last Action | Last Metric | Cost Today | Health |
|-------|-------|--------|-------------|-------------|------------|--------|
| alma | claude-opus-4-7 | active | - | None | $0.45 | OK |
| eval | claude-sonnet-4-6 | active | - | None | $0.00 | OK |
| hex | codex-mini-latest | active | - | None | $0.00 | OK |
| lux | gpt-5.4 | active | - | None | $0.00 | OK |
| perf-optimizer | codex-mini-latest | active | created | None | $0.00 | OK |
| rook | claude-sonnet-4-6 | active | - | None | $0.73 | OK |
| self-improve | codex-mini-latest | active | - | 220.0 | $0.00 | OK |

## Experience Replay

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
