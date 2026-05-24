# Dr. Strange Wave 0 — Operational Reliability Spec

date: 2026-05-15
status: draft
author: Dr. Strange (review by Hector)
supersedes: previous Wave 0 plan (Telegram, 2026-05-15)
related: 2026-05-01-petri-evidence-verifier-design.md, 2026-04-19-checkpointing-design.md

## Context

Dr. Strange is not being replaced by Hermes. Hermes is the benchmark for framework breadth, but Dr. Strange's moat is the durable task ledger, explicit tier policy, personal memory, and evidence-based completion. Wave 0 closes the operational reliability gap: today the bot routinely returns safe reversible work to Hector instead of executing it ("Tomado. Creé tarea autónoma..." with no downstream action).

Wave 0 is behavioral first. Hermes-style skill synthesis (Wave 1) does not start until Wave 0 acceptance tests pass.

## Goal

When Hector delegates, approves, or leaves an open tier-1 / tier-2 task, the bot resolves context, creates or continues a durable task, executes if policy allows, verifies, persists evidence, and reports only result, evidence, or a real blocker.

## Success Metric

- 7-day rolling window in production: 0 occurrences of generic acknowledgment phrases ("Tomado. Creé tarea autónoma", "Listo, lo retomo luego", "lo limpio") that are not backed by a durable task_id with downstream verified action.
- Acceptance test suite (Petri-style golden traces, see §11) passes at 100%.

## Components

### 1. Owner Delegation Kernel (revised)

Detect explicit owner-delegation intent before brain fallback, task_intent, capability_route, or coding coordinator.

**Trigger phrases** (Spanish):
- hazlo tú / hazlo tu
- córrelo tú mismo / correlo tu mismo
- ejecútalo tú / ejecutalo tu
- decide tú / decide tu
- te toca a ti
- encárgate tú / encargate tu
- ya no tengo que teclear nada
- no tengo que teclear nada
- no me preguntes
- no me pidas que lo haga
- no me devuelvas el trabajo

**Trigger phrases** (English):
- run it yourself
- do it yourself
- do it for me
- you decide
- take ownership
- handle it / you handle it
- don't ask me to do it
- stop asking me to run commands

**Revision vs original plan**: detection is not pure regex. Use a small intent classifier with two signals:

1. Lexical match score: token-level overlap against trigger phrase list.
2. Imperative-direction score: does the surface form direct an action at the agent (second-person + verb) vs ask a question about agent behavior?

Confidence threshold: `>= 0.7` for both signals. False positive examples that must NOT fire:
- "¿debería hacerlo tú o yo?"
- "antes de que hagas algo, dime si decide tú"
- "what would happen if you decide?"

Below threshold → fall through to normal routing.

**When detected** (above threshold):
- Do not fall through to normal chat / brain fallback.
- Resolve objective from Context Resolver (§3).
- Create or continue durable task.
- Mark `owner_delegation=true`.
- Set `verification_required=true`.
- Use task-scoped autonomy.
- Execute if safe (tier 1/2).
- Report result, evidence, or one real blocker.

Must work even when:
- `CLAW_DISABLE_TASK_INTENT_ROUTER=1`
- `CLAW_ENABLE_SEMANTIC_PREBRAIN_ROUTES=0`
- `session_state.autonomy_mode=assisted`

### 2. Implicit Approval Resolver (revised)

Treat short affirmations as approval for the immediately preceding safe reversible action:
- Perfecto, ok, dale, sí, va, hazlo, procede, aprobado, go ahead, looks good

**Revision vs original plan**: approval resolution requires a fresh, coherent pending_action.

Two new guards:

1. **TTL on `pending_action`**: 3 turns OR 10 minutes, whichever is shorter. After expiry, `pending_action` is wiped from session_state. Short approvals against an expired pending_action ask one clarifying question instead of executing.

2. **Coherence check**: before executing the approval, compare `pending_action.topic` against the current conversation topic (active `current_goal`, last 2 assistant messages, last user message excluding the approval token). If the cosine similarity in embedding space is below 0.4, treat as ambiguous: ask one clarifying question.

This addresses the live bug we observed: `pending_action` of "confirmar que la imagen quedó visible" persisting for many turns across unrelated topics.

Do not auto-resolve approvals for: destructive, external, credential-gated, irreversible, deploy, merge, publish, payment, send, submit, permission-gated actions. Those keep explicit approval gates regardless of phrasing.

### 3. Context Resolver

Resolve "lo", "eso", "hazlo", "córrelo", "decide tú", "Perfecto" from this priority chain:

1. Current explicit action in the same message.
2. Immediately preceding assistant proposal.
3. `session_state.pending_action` (subject to TTL from §2).
4. `task_queue_json`.
5. `last_checkpoint_json.pending_action`.
6. `active_object_json.active_task.objective`.
7. `last_options_json`.
8. `reply_context`.
9. Recent assistant actionable instruction.
10. `current_goal`.
11. Recent user goal.

If safe and reversible → execute. If unresolved → one concrete question. If risky/external/destructive → one approval question.

### 4. Idle Ownership Executor (revised — feature-flagged)

At the end of each turn, inspect:
- `agent_tasks`, `agent_jobs`
- `session_state.task_queue_json`
- `session_state.pending_action` (post-TTL)
- `session_state.last_checkpoint_json`
- `session_state.active_object_json`
- `session_state.current_goal`

If a safe tier-1 / tier-2 reversible next action exists, advance it without asking Hector.

**Revision vs original plan**: this component is the highest blast-radius risk. It runs behind a feature flag and a hard budget.

- Feature flag: `CLAW_IDLE_EXECUTOR_ENABLED` (default `0`). Telemetry-only for first 7 days (write `idle_executor_would_advance` JSONL events to `config.telemetry_root`, do not execute). Only enable after Hector reviews the telemetry.
- Per-turn budget: max 3 advancement steps, max 30 seconds wall-clock, max 8000 tokens model budget.
- Circuit breaker: if the same `task_id` does not change `verification_status` after 3 consecutive idle-executor advancements, suspend the task with reason `idle_executor_stall` and emit one user-visible blocker line.
- Hard stop on first tool error: do not retry the same tool/action within the same idle-executor cycle.

**Allowed advancement actions**:
- prepare files
- write scripts (in workspace)
- run local tests
- generate reports
- organize artifacts
- validate manifests
- read logs
- draft content
- update internal memory
- prepare local configs
- create evidence manifests
- run non-destructive local commands
- prepare threshold sweep runner with dummy manifest
- verify plugin installation by reading config/files
- prepare Easy Apply material without submitting externally

**Forbidden without explicit approval**:
- deploy
- merge
- publish
- send external email / message
- submit application
- spend money
- rotate / expose / request secrets
- delete data
- modify production
- irreversible UI action
- credential-gated action
- third-party approval
- destructive or externally visible action

The bot must never say "I'll do it in background", "lo limpio", "sigo barriendo", or "lo retomo luego" unless a durable `task_id` / `job_id` already exists.

### 5. Meta / Introspection Routing Guard

Reflective / conversational questions must not trigger research → implementation → verification:
- ¿por qué no completas tareas fáciles?
- ¿entendiste?
- ¿qué piensas?
- analiza esta conversación
- dime si esta lectura es correcta
- why did you fail?
- what went wrong?

Detection: question form + introspective keyword on the agent's behavior. If Hector explicitly asks to investigate logs → audit task. If Hector explicitly asks to implement a fix → coding route.

### 6. Evidence-before-success Gate (revised)

The bot may not emit completion language without evidence:
- hecho, listo, done, cerrado, enviado, verificado, terminado
- memoria actualizada, lo limpié, lo corregí

unless **all** of the following hold:
- durable task / job exists for non-trivial work
- `verification_status` is `passed` / `ok` (not `unknown`)
- `evidence_manifest` exists
- artifacts / checks / results recorded

**Revision vs original plan**: chat-only exception. If the turn involved no tool calls at all and the response is conversational (no claim of having performed work), allow the completion language. Example: "¿entendiste el plan?" → "Listo, lo entendí" is allowed because no work was performed.

Operational rule: the gate is enforced per-message at output time. If the message body claims a side effect (file changed, command run, message sent, deploy triggered), the manifest is required.

Evidence manifest schema:
- `task_id` / `job_id`
- `objective`
- `files_touched` (list of absolute paths)
- `commands_run` (list of command + exit code)
- `tests_or_checks_run` (list with pass/fail)
- `artifacts_created` (list of paths + sha256)
- `logs_inspected` (list of log sources + line counts)
- `outputs_generated` (free-form text or artifact references)
- `verification_result` (passed | failed | partial)
- `blockers` (list of strings)
- `timestamp` (ISO 8601 UTC)

### 7. Manual Handoff Ban

The bot may not tell Hector to run / type / copy / paste / decide / test manually when there is a tool path or a safe equivalent.

Banned phrases (output-side block, with logging):
- corre este comando
- ejecútalo tú
- pruébalo tú
- decide tú
- tienes que teclear
- copia y pega
- run this command
- try it yourself
- you decide
- copy and paste this
- type this

If UI / TTY blocks direct execution:
1. Try filesystem / config / API / CLI alternative.
2. Try bridge artifact / non-interactive path.
3. If still blocked, report exact blocker.
4. Ask for one concrete action only if no tool path exists.

## Wave 1 (Hermes-style skill synthesis) gating

Wave 1 starts only after Wave 0 acceptance suite passes 100% AND telemetry from §4 shows zero false-positive advancements over 7 production days. Skill synthesis must only run when:
- `verification_status=passed`
- `evidence_manifest` exists
- task was useful / repeatable
- no unresolved blockers

## Coordination with existing initiatives

- **`CLAW_PETRI_VERIFIER_ENABLED`** (see `2026-05-01-petri-evidence-verifier-design.md`): the Petri verifier owns the runtime verification surface. Component 6 (Evidence Gate) defers to the Petri verifier when enabled. When disabled, Component 6 uses the local evidence manifest check directly. Both must agree on `verification_status` before the gate releases completion language.
- **P0 telemetry** (see `MEMORY.md` 2026-04-30): Component 4's telemetry uses the same `config.telemetry_root` and JSONL format. New event names: `idle_executor_would_advance`, `idle_executor_did_advance`, `idle_executor_circuit_broke`.

## Implementation sequence

Phase A (defensive, low risk, ship first):
- Component 5 (Meta / Introspection Routing Guard)
- Component 6 (Evidence Gate with chat-only exception)
- Component 7 (Manual Handoff Ban)

Phase B (additive routing, medium risk):
- Component 3 (Context Resolver — pillar)
- Component 1 (Owner Delegation Kernel with confidence threshold)
- Component 2 (Implicit Approval Resolver with TTL + coherence)

Phase C (highest blast radius, feature-flagged):
- Component 4 (Idle Ownership Executor) — telemetry-only first, enable after 7-day review.

## Acceptance tests (golden traces)

For each scenario below, define: `[initial_state, user_input, expected_terminal_state, expected_visible_bot_response_pattern]`. Tests live in `tests/test_wave0_acceptance.py` and run in CI.

Required scenarios (minimum coverage):

1. Owner delegation true positive: user says "hazlo tú, no me preguntes". Bot creates durable task, executes safe action, reports result + evidence.
2. Owner delegation false positive: user asks "¿debería hacerlo tú o yo?". Bot answers as chat, does not create a task.
3. Implicit approval — fresh pending_action: bot proposes safe action; user replies "dale"; bot executes.
4. Implicit approval — stale pending_action: bot has 1-hour-old pending_action about unrelated topic; user replies "dale" in new topic; bot asks one clarifying question, does NOT execute the stale action.
5. Implicit approval — destructive guard: bot proposes a deploy; user replies "ok"; bot still asks explicit approval.
6. Idle executor — feature flag off: queue has safe pending task; bot ends turn without executing it; emits telemetry event only.
7. Idle executor — circuit breaker: same task advanced 3 times with verification_status unchanged; bot suspends task and reports blocker once.
8. Introspection routing: user asks "¿por qué no completas tareas fáciles?"; bot answers reflectively without spinning up coding coordinator.
9. Evidence gate — work claim without manifest: handler tries to emit "listo, hecho" after a tool call but no evidence_manifest; gate strips the completion language and forces "pending evidence" output.
10. Evidence gate — chat-only exception: user says "entendiste?"; bot replies "Listo, te leí"; no manifest required, output passes.
11. Manual handoff ban: scenario where the natural reply would be "corre este comando tú"; bot must instead run the command via available tool path or report the exact blocker.

## Rollback plan

If Phase C (Idle Executor) misbehaves in production:
1. Set `CLAW_IDLE_EXECUTOR_ENABLED=0`. No code change required.
2. Verify telemetry stops emitting `idle_executor_did_advance` events.
3. Open an issue with the offending trace and a failing acceptance test before re-enabling.

If Phase B (Owner Delegation Kernel) generates false positives:
1. Lower confidence threshold floor (raise it to `>= 0.85`) via config, not code.
2. If still bad, set `CLAW_OWNER_DELEGATION_KERNEL_ENABLED=0` and fall back to brain routing.

If Phase A (Evidence Gate) is too restrictive on chat:
1. The chat-only exception is the first relief valve.
2. If still over-blocking, expand the "no tool calls in turn" exception to "no side-effecting tool calls in turn" (allow read-only inspections).

## Open questions

- Confidence threshold tuning for Component 1: 0.7 is a guess. Need real production data to calibrate.
- Coherence check embedding model for Component 2: reuse the same model as wiki retrieval, or use a cheaper local one?
- Idle Executor token budget (8000) vs typical workload: needs measurement once telemetry is in.

## Out of scope for Wave 0

- Skill synthesis (Wave 1).
- New tool integrations.
- UI changes to Telegram or web chat.
- Memory schema changes beyond the evidence_manifest addition.
