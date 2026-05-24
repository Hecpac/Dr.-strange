# Dr. Strange AgentSpec Integration — Runtime Rule Enforcement

date: 2026-05-15
status: draft
author: Dr. Strange (review by Hector)
related: 2026-05-15-dr-strange-wave-0-design.md, 2026-05-01-petri-evidence-verifier-design.md
references: AgentSpec paper (arXiv:2503.18666v3), Wang/Poskitt/Sun, ICSE'26

## Context

Dr. Strange's tier policy (Tier 1/2/2.5/3) is enforced today in two layers:

- **Text in `SOUL.md`** — instructions to the brain, which the model can ignore under prompt injection or sycophancy.
- **Code in `claw_v2/tools.py`** (lines 134–177) — static tier assignment per tool, with `ApprovalGate` callable for Tier 3 routing.

This works for "tool X requires approval", but cannot express dynamic conditions like "this `git_push` is fine because the branch is `feature/foo`" or "this `Bash` command is unsafe because its argv contains a secret". The brain sees the policy as text; the dispatcher sees only a flat tier number.

AgentSpec (Wang, Poskitt, Sun — ICSE'26) provides a lightweight DSL for runtime rules with the shape `rule / trigger / check / enforce`. The paper reports >90% prevention of unsafe executions in code agents with millisecond overhead. This spec adopts AgentSpec into Dr. Strange to:

1. Express tier policy as evaluable rules, not prompt text.
2. Add context-aware conditions (path, branch, argv contents, result content).
3. Provide a shadow-first promotion path so we never block in production without 14 days of telemetry.
4. Close OWASP Agentic Top 10 risks at the dispatcher boundary instead of relying on the brain.

## Goal

Make every tool call evaluable against a deterministic rule set before and after execution, without rewriting the existing approval flow. Tier policy lives in rules; the dispatcher stays the only place where rules fire.

## Non-goals

- Replace `ApprovalManager` / `ApprovalGate`. AgentSpec rules with `enforce user_inspection` reuse the existing approval flow.
- Replace the brain. AgentSpec is deterministic; the brain still decides intent.
- Cover non-tool-call surfaces in v1 (e.g. raw memory writes, raw network egress). Those are v2.

## Success metric

- 14-day shadow run: 0 false positives on production traffic for the seed rule pack (defined in §5). A false positive = a rule that fires on a legitimate, expected, safe action.
- Post-promotion: 0 silent failures on Tier 3 actions (every Tier 3 either goes through `user_inspection` or is `block`ed by a rule).
- Telemetry coverage: every tool call (before + after) emits exactly one `agentspec_evaluation` event with the rule outcomes.

## Architecture

### Rule structure (DSL)

Per AgentSpec paper:

```
rule <unique_id>
    trigger <event_name>
    check <predicate_1> ∧ <predicate_2> ∧ ...
    enforce <action>
```

In Python implementation (see §3):

```python
@dataclass
class Rule:
    id: str
    trigger: str
    check: Callable[[EvalContext], bool]
    enforce: EnforceAction  # block | user_inspection | llm_self_examine | modify | log
    description: str = ""
```

### Triggers (seed list)

- `before_tool_call` — fires before the dispatcher invokes a tool definition.
- `after_tool_call` — fires after the tool returns a result, before the result is fed back to the brain.
- `before_memory_save` — fires before `memory.save_fact` / `memory.save_lesson` writes.
- `before_assistant_output` — fires on the final user-facing text emitted by `bot.py:handle_text`, before it reaches the channel transport.
- `on_goal_change` — fires when `session_state.current_goal` is mutated.

Each trigger ships an `EvalContext` with typed fields. Example for `before_tool_call`:

```python
@dataclass
class BeforeToolCallContext:
    tool_name: str
    tool_tier: int
    args: dict
    session_id: str
    objective: str | None  # active task objective
    user_turn_text: str | None  # last user message
    has_explicit_user_request: bool  # did the current turn ask for this action?
```

### Enforce actions

- `log` — record the evaluation, do not interfere. Shadow mode.
- `block` — raise `AgentSpecBlocked(rule_id, reason)`. The dispatcher converts it to a user-visible blocker line and records the event.
- `user_inspection` — raise `ApprovalPending(...)` reusing the existing flow. The dispatcher hands off to `ApprovalManager` exactly as today, so the Telegram `/approve` UX is unchanged.
- `llm_self_examine` — invoke a secondary LLM (Codex GPT-5.5 verifier by default, see Petri verifier spec) to evaluate the proposed action. If the secondary says unsafe → `block`. If safe → allow.
- `modify` — return a new `args` dict. The dispatcher continues with the modified args. Used for arg sanitization (redact secret from argv, force path canonicalization).

### Integration point

Today's dispatcher flow (simplified, from `claw_v2/tools.py`):

```
ToolRegistry.invoke(definition, args)
  → approval_gate(definition, args)     # raises ApprovalPending if Tier 3
  → handler(definition, args)
  → record_outcome
```

Post-AgentSpec flow:

```
ToolRegistry.invoke(definition, args)
  → agentspec.evaluate("before_tool_call", ctx)   # NEW
       → may raise AgentSpecBlocked
       → may raise ApprovalPending (via user_inspection)
       → may return modified args
  → approval_gate(definition, args)               # legacy gate still runs
  → handler(definition, args)
  → agentspec.evaluate("after_tool_call", ctx_with_result)   # NEW
       → may raise AgentSpecPostBlocked (discard result)
  → record_outcome
```

The legacy `approval_gate` stays because:
- Static Tier 3 tools (`HeyGenVideo`, `GPTImage`, `SkillExecute`, `WikiDelete`, `A2ASend`) still need flat-tier approval even if no AgentSpec rule fires.
- Defense in depth — AgentSpec is a new layer, not a replacement.

When AgentSpec triggers `user_inspection`, it raises the same `ApprovalPending` exception, so from the dispatcher's POV the two layers compose without changes upstream.

## Predicate library (v1)

Reusable predicates that rules can compose. Each is a pure function over `EvalContext`.

| Predicate | Signature | Purpose |
|---|---|---|
| `path_inside_workspace` | `(path: str) → bool` | path is under `WORKSPACE_ROOT` |
| `path_in_blocklist` | `(path: str, blocklist: list) → bool` | path matches a forbidden glob (e.g. `.env`, `**/credentials.json`) |
| `branch_is_protected` | `(branch: str) → bool` | branch in `{main, master, prod, production, release/*}` |
| `tool_in_set` | `(tool_name: str, allowed: set) → bool` | typed membership |
| `argv_contains_secret_marker` | `(argv: list) → bool` | argv contains regex for token/key/secret patterns |
| `result_contains_instruction_pattern` | `(text: str) → bool` | result matches `/ignore (previous\|all\|prior)\|disregard\|act as\|new instruction/i` |
| `has_explicit_user_request` | `(turn: str, action_kw: list) → bool` | current user turn explicitly asked for this action |
| `objective_mentions` | `(objective: str, terms: list) → bool` | active task objective mentions the action type |
| `is_destructive_command` | `(argv: list) → bool` | argv matches `rm -rf`, `git push --force`, `truncate`, etc. |
| `url_in_allowlist` | `(url: str, allowlist: list) → bool` | network egress hostname allowed |

All predicates ship with unit tests. The library lives at `claw_v2/agentspec/predicates.py`.

## Seed rule pack (v1)

```
rule write_outside_workspace
    trigger before_tool_call
    check tool_name ∈ {"Write", "Edit"}
        ∧ ¬ path_inside_workspace(args.path)
    enforce block
    description: "Block writes outside WORKSPACE_ROOT; defends against path traversal in args."

rule write_to_secret_file
    trigger before_tool_call
    check tool_name ∈ {"Write", "Edit"}
        ∧ path_in_blocklist(args.path, [".env", "**/credentials.json",
                                         "**/*.pem", "**/*.key", "**/.git/**"])
    enforce block
    description: "Block writes to known-secret paths."

rule bash_destructive_command
    trigger before_tool_call
    check tool_name == "Bash"
        ∧ is_destructive_command(args.command)
        ∧ ¬ has_explicit_user_request(user_turn_text, ["delete", "borrar", "wipe"])
    enforce user_inspection
    description: "Destructive Bash commands need explicit approval when user did not ask."

rule git_push_protected_branch
    trigger before_tool_call
    check tool_name == "Bash"
        ∧ args.command matches /git push.*\b(main|master|prod|production)\b/
        ∧ ¬ has_explicit_user_request(user_turn_text,
              ["push", "merge", "deploy", "publica"])
    enforce user_inspection
    description: "Push to protected branch needs explicit current-turn request (Tier 2.5)."

rule goal_hijack_in_tool_result
    trigger after_tool_call
    check result_contains_instruction_pattern(result.text_excerpt)
    enforce block ∧ log_security_event
    description: "Tool result contains attempted goal hijack; discard and log (OWASP Agentic #1)."

rule memory_save_from_untrusted_source
    trigger before_memory_save
    check origin ∈ {"web_fetch", "wiki_context",
                    "learned_lesson", "user_paste"}
        ∧ ¬ has_human_verification_flag(payload)
    enforce llm_self_examine
    description: "Untrusted-origin saves get a second-LLM sanity check (OWASP Agentic #5)."

rule manual_handoff_phrase_in_output
    trigger before_assistant_output
    check output_text matches /corre este comando|ejecútalo tú|pruébalo tú|run this command|copy and paste this|type this|you decide/i
        ∧ ¬ no_tool_path_available(intent)
    enforce block
    description: "Wave 0 component 7 at engine level — block manual-handoff phrases when tools exist."

rule untrusted_url_egress
    trigger before_tool_call
    check tool_name ∈ {"WebFetch", "Bash"}
        ∧ (tool_name == "WebFetch" ? ¬ url_in_allowlist(args.url, NETWORK_ALLOWLIST)
                                    : args.command matches /curl|wget/ ∧ ¬ url_in_allowlist(extract_url(args.command), NETWORK_ALLOWLIST))
    enforce log  # phase-1 shadow; promote later
    description: "Track unexpected network egress; promote to block after 14d telemetry."
```

8 rules total. Each is exercised by `tests/test_agentspec_seed_rules.py`.

## Telemetry

Every evaluation emits one JSONL event to `config.telemetry_root/agentspec.jsonl`:

```json
{
  "ts": "2026-05-15T14:23:11Z",
  "session_id": "tg-574707975",
  "trigger": "before_tool_call",
  "tool_name": "Bash",
  "rule_id": "git_push_protected_branch",
  "predicates": {
    "tool_is_bash": true,
    "argv_matches_push_protected": true,
    "has_explicit_user_request": false
  },
  "all_predicates_passed": true,
  "enforce_action": "user_inspection",
  "actual_outcome": "approval_pending_created",
  "approval_id": "appr_..."
}
```

Mode is recorded so shadow vs enforce are distinguishable:

```json
{ "mode": "shadow_log", "would_have_enforced": "block", "actual_action_taken": "allowed" }
```

## Promotion path (shadow → enforce)

Phase A — **All rules in `log` mode** for 14 days of production traffic. Inspection criteria:

- `would_have_blocked` events / day per rule. If > 0 on legitimate traffic → tune predicates, do not promote.
- `would_have_blocked` events on actual attack-shaped inputs → ship-blocking signal, prioritize.
- Latency per evaluation. Target: p99 < 5ms. If a rule's predicates are slow, refactor.

Phase B — **Hard rules promoted to enforce**:
- `write_outside_workspace` → `block`
- `write_to_secret_file` → `block`
- `goal_hijack_in_tool_result` → `block`
- `manual_handoff_phrase_in_output` → `block`

Phase C — **Soft rules promoted to enforce**:
- `bash_destructive_command` → `user_inspection`
- `git_push_protected_branch` → `user_inspection`
- `memory_save_from_untrusted_source` → `llm_self_examine`

Phase D — **Shadow promotions evaluated**:
- `untrusted_url_egress` → re-evaluate after 14d. May need allowlist tuning before block.

Each promotion is a single-line PR (rule's `enforce` field). Reversible.

## File layout

```
claw_v2/agentspec/
    __init__.py           # Engine entrypoint: AgentSpecEngine
    engine.py             # Evaluation loop, trigger dispatch, telemetry
    rules.py              # @dataclass Rule, EnforceAction enum
    predicates.py         # Reusable predicate library
    seed_rules.py         # The 8 seed rules from §5
    context.py            # EvalContext dataclasses per trigger
    exceptions.py         # AgentSpecBlocked, AgentSpecPostBlocked
tests/
    test_agentspec_engine.py
    test_agentspec_seed_rules.py
    test_agentspec_predicates.py
    test_agentspec_integration_dispatcher.py
docs/superpowers/specs/
    2026-05-15-dr-strange-agentspec-integration-design.md   # this file
```

The reference Python implementation at https://github.com/haoyuwang99/AgentSpec is adapted, not vendored. Vendor would create a maintenance burden; we lift the DSL shape and re-implement against Dr. Strange's existing dataclasses.

## Coordination with Wave 0 and Petri Verifier

- **Wave 0 Component 7 (Manual Handoff Ban)** is implemented as the AgentSpec rule `manual_handoff_phrase_in_output`. The Wave 0 spec describes the policy; this spec describes the mechanism. No duplication.
- **Wave 0 Component 6 (Evidence Gate)** stays separate. The Evidence Gate runs on assistant *claims of completion*; AgentSpec runs on *tool calls and outputs*. They compose: AgentSpec stops bad actions; Evidence Gate stops false reports about actions.
- **Petri Verifier** (`CLAW_PETRI_VERIFIER_ENABLED`) is the natural backend for `enforce llm_self_examine`. When the flag is on, `llm_self_examine` delegates to the Petri verifier instead of a fresh secondary LLM call. When off, fall back to a single Codex call.

## Rollback plan

- Per-rule rollback: revert one rule to `enforce log`. Single field change, no code change.
- Engine-wide kill switch: env `CLAW_AGENTSPEC_ENABLED=0` short-circuits `AgentSpecEngine.evaluate` to return `Allow()` immediately. No tool call path changes.
- Database/telemetry rollback: telemetry events are JSONL only; truncating the file is safe. No DB schema changes in v1.

## Open questions

- Predicate `result_contains_instruction_pattern` regex is heuristic. Will it false-positive on legitimate "ignore" tokens (e.g. `.gitignore`)? Need 14d telemetry to calibrate.
- `llm_self_examine` adds latency (one extra Codex call). For high-frequency triggers it may need a cache or a confidence threshold to skip examination.
- `before_assistant_output` runs at message send time — does it interact badly with streaming responses? In v1 only the final compacted output is evaluated; streaming chunks bypass. Revisit when streaming is wired.

## Out of scope for v1

- Memory egress rules (read of secrets out of memory). Add in v2 once we add per-fact origin tagging.
- Inter-agent communication rules (A2ASend, Skill→Skill). v2.
- LLM-generated rules. v3 after seed pack is proven stable.
- DSL parser for human-authored rules in `.rule` files. v1 stays in Python dataclasses; humans write rules as code. A YAML or DSL frontend is a v2 ergonomics improvement.

## Implementation order

1. `claw_v2/agentspec/{rules,context,exceptions,engine}.py` skeleton + unit tests.
2. `predicates.py` with 10 predicates + unit tests.
3. `seed_rules.py` with the 8 rules, all in `enforce log` mode.
4. Wire `engine.evaluate("before_tool_call", ...)` and `engine.evaluate("after_tool_call", ...)` into `tools.py` dispatcher.
5. Wire `engine.evaluate("before_memory_save", ...)` into `memory.py` save paths.
6. Wire `engine.evaluate("before_assistant_output", ...)` into `bot.py:handle_text` just before reply send.
7. Telemetry sink to `config.telemetry_root/agentspec.jsonl`.
8. Integration test: simulate a path-traversal `Write`, verify the engine catches it.
9. Ship behind `CLAW_AGENTSPEC_ENABLED=1` flag, log mode only.
10. After 14d of production telemetry, follow promotion path from §7.

Estimated effort: 3–5 days for items 1–9. Promotion is a separate, calendared activity.
