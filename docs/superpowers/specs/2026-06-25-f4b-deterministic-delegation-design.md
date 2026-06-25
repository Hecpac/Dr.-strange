# F4-B1 — Deterministic delegation for high-confidence authenticated browse intents (Design)

Status: Design, 2026-06-25. Scope = F4-B1 only. NOT F4-B2 (post-model anti-confabulation / forced-action loop). Behind a dedicated flag, default OFF.

## Problem (verified)
"Haz un repaso por X" → brain emitted **zero** tool calls, no job, and confabulated a `ToolSearch`/`tool_policy` rejection that never happened (`ToolSearch` doesn't exist in claw_v2; `mcp__claw__delegate_task` is brain-allowed and worked 2026-06-24). Root cause = model failure-to-act, not a policy/registry defect. Cure: for a narrow set of unambiguous "review my authenticated X feed" intents, route deterministically and enqueue the durable background job **without** depending on the model to call a tool.

## Design properties (from the brief)
1. Dedicated narrow flag, default OFF: **`CLAW_F4_DETERMINISTIC_DELEGATION`**.
2. Narrow, conservative, pure-unit classifier (prefer false negatives).
3. Deterministic enqueue via the existing delegation boundary (`TaskHandler.start_autonomous_task`), not a hand-rolled job.
4. Exactly-once on inbound delivery identity.
5. Truthful failure (no fabricated tool/policy detail).
6. Observability events.
7. Runtime parity when OFF.

## Verified facts driving the design
- `handle_text(user_id, session_id, text, runtime_channel, context_metadata)` runs ~20 `_maybe_handle_*` dispatchers; non-None return = capture (early return), None = fall through (`bot.py:3523`, `_handle_text_body`).
- `task_intent` and `capability_route` dispatchers are `disabled_by_flag` (`CLAW_DISABLE_TASK_INTENT_ROUTER=1`).
- Delegation boundary: `start_autonomous_task(session_id, objective, *, mode, source_text, task_kind, ..., delegation_metadata) -> str` (`task_handler.py:321`) → `_enqueue_autonomous_job` → `JobService.enqueue(kind="coordinator.autonomous_task", resume_key=_resume_key_for_task(task_id))`. Fresh `task_id = session_id:time_ns()` each call → **not idempotent across calls**.
- `JobService.enqueue` **dedups on `resume_key`**: `if resume_key: existing = get_active_by_resume_key(resume_key)` (`jobs.py:159-160`); DB `UNIQUE INDEX idx_agent_jobs_active_resume_key ON agent_jobs(resume_key) WHERE resume_key IS NOT NULL` (`jobs.py:58-60`). This is the durable, concurrency-safe exactly-once primitive.
- Telegram transport passes `context_metadata=_reply_context_metadata(update)` (`telegram.py:1471`) which is `None` for a normal message and only carries reply text — **no `message_id`/`update_id` reaches `handle_text` today**. The ids exist on `update` (`update.update_id`, `update.message.message_id`).

## Architecture

### A. Delivery-id plumbing (transport → handle_text)
Add a stable per-delivery id to `context_metadata` so the gate can dedup. In `telegram._handle_text`, merge `{"inbound": {"channel": "telegram", "message_id": <int>, "update_id": <int>}}` into the dict passed as `context_metadata` (preserving the existing `reply_context`). `handle_text`/`_handle_text_body` already forward `context_metadata` unchanged — no signature change there. Web-chat and other channels that don't supply an id simply omit `inbound.message_id`; the gate then **does not deterministically enqueue** (falls through) — never enqueue without a dedup id.

### B. Narrow classifier (pure function, new module `claw_v2/delegation_intents.py`)
`classify_authenticated_browse_intent(text) -> BrowseIntent | None`. Conservative: matches only unambiguous "review/sweep my authenticated X/Twitter feed/timeline" phrasings. Approach: normalize (lowercase, strip accents); require BOTH a review-verb token (`repaso|repasar|barrido|barre|revisa|revisá|revisar|chequea|chequeá|dale una vuelta|echa un vistazo|mirá|mira`) AND an X-feed target (`\bx\b|twitter|mi feed|timeline|mi tl`), AND must NOT contain authoring/definitional/opinion tokens (`escrib|redacta|postea|publica|qué es|que es|qué opinas|que opinas|resume|resumen|borrador|draft`). Must match `"Haz un repaso por X"`; must reject the four listed non-matches. Returns a small dataclass with `objective` (a normalized delegation objective string) + `kind`.

### C. Gate dispatcher (`bot.py`)
New `_maybe_handle_f4_deterministic_delegation(text, *, session_id, context_metadata) -> str | None`, inserted in `_handle_text_body` **before** `task_intent`/`capability_route` (so if the broad router is ever re-enabled, this captures first → no double handling). Flow:
1. If `not config.f4_deterministic_delegation` → return None (parity OFF).
2. `intent = classify_authenticated_browse_intent(text)`; if None → return None (fall through).
3. Extract `message_id` from `context_metadata["inbound"]`; if absent → emit `f4_deterministic_delegation_skipped_no_delivery_id` and return None (no enqueue without dedup id; brain still handles).
4. `resume_key = f"f4b-delegation:{session_id}:{message_id}"`.
5. Pre-check `job_service.get_by_resume_key(resume_key)` (**any status**, not just active — a *completed* job keeps the key because the unique index is `WHERE resume_key IS NOT NULL`, and `enqueue` re-raises `IntegrityError` on a completed-key collision; the any-status pre-check dedups before any side effect). If a job exists → emit `f4_deterministic_delegation_matched` (deduped=true) + return the same truthful ack (no new enqueue).
6. Else: `ack = task_handler.start_autonomous_task(session_id, intent.objective, mode="browser"|appropriate, source_text=text, task_kind="authenticated_browse", delegation_metadata={"source":"f4_deterministic_delegation","channel":...}, idempotency_key=resume_key)`. Emit `f4_deterministic_delegation_matched` + `f4_deterministic_delegation_enqueued` (safe ids only). Return a truthful ack ("Lancé el repaso de tu feed de X en una tarea de fondo; te aviso al terminar." + safe task ref).
7. On any exception in 6: emit `f4_deterministic_delegation_failed` (reason code only, no raw error), return a concise truthful failure ("No pude crear la tarea de fondo para el repaso de X; no quedó nada encolado." — no invented tool/policy detail, no "repeat the command"). Return non-None (captured) so the brain doesn't then confabulate.

### D. Idempotent enqueue (boundary param)
Add optional `idempotency_key: str | None = None` to `start_autonomous_task` and `_enqueue_autonomous_job`; pass it as `JobService.enqueue(resume_key=idempotency_key or _resume_key_for_task(task_id))`. Default None → exact current behavior. The gate pre-check (C5) handles the common duplicate before side effects; the DB unique index is the concurrency backstop (second concurrent enqueue returns the existing job).

### E. Config flag
`config.py`: `f4_deterministic_delegation: bool = _env_bool("CLAW_F4_DETERMINISTIC_DELEGATION", False)`. Default OFF. Does not touch `CLAW_DISABLE_TASK_INTENT_ROUTER`.

## Flag/router matrix
- Flag OFF → gate returns None immediately → exact existing behavior.
- Flag ON + broad router disabled (current prod) → narrow gate operates.
- Flag ON + broad router enabled → narrow gate runs FIRST and captures → broad router never sees the message → handled exactly once.

## Exactly-once
- Dedup identity = `(session_id, telegram message_id)` → `resume_key`.
- Same inbound message twice → same resume_key → pre-check or DB unique index → one job.
- New message, identical text → new message_id → new resume_key → new job.
- No delivery id → no deterministic enqueue (fall through).

## Truthful failure
No fabricated tool/policy/loader/node detail; a single `f4_deterministic_delegation_failed` event (reason code, no raw error/secrets); concise user message stating no task was created; no "send the same command again."

## Observability
`f4_deterministic_delegation_matched` (with `deduped`), `f4_deterministic_delegation_enqueued` (task ref, resume_key hash/safe), `f4_deterministic_delegation_failed` (reason code), `f4_deterministic_delegation_skipped_no_delivery_id`. Safe ids/reason codes only — no prompts, credentials, cookies, browser state.

## Files
- Create: `claw_v2/delegation_intents.py` (classifier) + `tests/test_f4b_deterministic_delegation.py`.
- Modify: `claw_v2/config.py` (flag), `claw_v2/bot.py` (gate + wire into `_handle_text_body`), `claw_v2/task_handler.py` (+ `_enqueue_autonomous_job` optional `idempotency_key`), `claw_v2/telegram.py` (inbound id in context_metadata), `tests/helpers.py` (config field), `claw_v2/INTERNAL_WIRING.md` (invariant + §5.1 + doc_version).

## INTERNAL_WIRING invariant
`high_confidence_delegation_intents_do_not_depend_on_model_tool_choice` — flag/default, narrow scope, broad-router-disabled interaction, deterministic enqueue boundary, exactly-once via resume_key, truthful failure, why re-prompt is insufficient, and that forced-action/anti-confabulation is F4-B2.

## Why not re-prompt (F4-B2 boundary)
A re-prompt re-enters the same model that just confabulated; it can be talked around and does not guarantee the tool call. Deterministic routing removes the enqueue from model discretion for the narrow, unambiguous case. Broader forced-action + post-model anti-confabulation stays F4-B2.

## Stop conditions checked
- Durable exactly-once boundary EXISTS (`JobService` resume_key + unique index). ✓ not blocked.
- No primary-DB writes required in this run (tests use temp DBs / fakes). ✓
- Duplicate-vs-legitimate-repeat distinguishable via Telegram message_id (plumbed). ✓
- Narrowness achievable (conservative classifier + explicit non-match tests). ✓
- No global re-enable of the task-intent router. ✓ (dedicated flag only)
