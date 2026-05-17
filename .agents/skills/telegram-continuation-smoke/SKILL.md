---
name: telegram-continuation-smoke
description: Use when auditing or fixing Dr. Strange Telegram continuation, approval hijacking, brain-first routing, NaturalLanguageRenderer leaks, or replay smokes involving "Crea una misión..." followed by "Procede", "Continúa", and "Dale".
metadata:
  short-description: Audit Telegram brain-first continuation routing
---

# Telegram Continuation Smoke

Use this workflow for P0 fixes where Telegram replies are being captured by routers, stale approvals, or generic continuation templates instead of being understood as a full turn.

## Required Checks

1. Classify the full inbound turn before reading approvals, pending tasks, reply context, or task ledger.
   Allowed intents: `new_task`, `continue_active_mission`, `approval_response`, `correction_or_behavior_instruction`, `question`, `debug_request`.
2. If the turn is `new_task` with a clear objective, create or update a durable task/proposal first. Do not let unrelated approvals or pending tasks hijack it.
3. Treat approvals as blockers only when scoped to the same chat and the same reply/thread/mission/task/action, or when the text is an explicit authorization/continuation.
4. Run normal-mode output through `NaturalLanguageRenderer` or equivalent presentation checks. Normal replies must not expose `approval_id`, `task.contextual_action`, `needs_approval`, `waiting_for_user_input`, `explicit_blocker`, internal command strings, or raw task IDs unless the user asked for debug/audit.
5. For real ambiguity, ask a specific choice question. Never repeat a generic clarification after the user gave a clear objective.
6. Emit one trace per turn with: `semantic_intent`, `state_sources_checked`, `approval_scope_match`, `decision`, `output_kind`, `leaked_internal_labels`.

## Replay Smoke

Seed at least one unrelated pending approval, then send:

```text
Crea una misión durable de prueba llamada audit-continuation-smoke...
```

Expected:

- Semantic trace says `new_task`.
- A durable task/proposal exists.
- The visible reply is natural and asks for `Procede`.
- No unrelated approval ID, approval command, or internal label appears.

Then send:

```text
Procede
Continúa
Dale
```

Expected:

- Each turn resolves against active mission/task/proposal context.
- No generic "qué acción concreta" loop when context is available.
- If execution cannot proceed, the reply is a scoped approval, explicit blocker, durable task, or specific clarification.

## Focused Verification

Run:

```bash
.venv/bin/python -m pytest tests/test_brain_first_semantic.py tests/test_telegram_imperative_router.py -q
```

For boot/memory-sensitive changes also run:

```bash
.venv/bin/python -m pytest tests/test_workspace.py tests/test_lifecycle.py tests/test_brain_core.py tests/test_memory_core.py -q
```
