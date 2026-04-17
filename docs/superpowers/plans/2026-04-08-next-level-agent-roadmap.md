# Next-Level Agent Roadmap — Implementation Plan

> **For agentic workers:** Use this roadmap as the execution order. Do not parallelize work across phases unless the listed dependencies are already satisfied. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Elevate Claw from a capable multi-surface bot into a robust agent platform with durable execution, measurable quality, capability-aware orchestration, and safer autonomy.

**Summary:** The repo already has the right primitives: multi-lane routing, approvals, memory, sub-agents, coordinator, browser/computer use, pipeline orchestration, and notebook integrations. The next level is not more commands. It is stronger contracts, evals, traceability, and durable jobs.

**Primary bet:** `evals + traces + replay + durable jobs` first. This gives every future improvement a measurable feedback loop and lowers regression risk.

**Dependencies:** Phase 0 must land before any serious autonomy expansion. Phase 1 depends on Phase 0 traces and typed artifacts. Phase 2 depends on Phase 1 evals and job durability.

---

## Success Criteria

- [ ] Every agent action runs under a typed lifecycle: `plan -> execute -> verify -> outcome`.
- [ ] Long-running tasks survive process restart and can be resumed or cancelled.
- [ ] Every important workflow has replayable traces and eval coverage.
- [ ] Router decisions are measurable by quality, latency, and cost, not just lane defaults.
- [ ] Inter-agent work is dispatched by capability and policy, not hardcoded names.
- [ ] Unsafe autonomy is bounded by explicit risk policy and approval state.

---

## Phase 0 — Make The Runtime Operable

### Outcome

Claw has a durable execution model, end-to-end traces, and a single runtime contract for agents/jobs/actions. This is the minimum foundation for raising autonomy safely.

### P0.1 Typed execution artifacts

**Goal:** Stop relying on loose event payloads as the only source of truth.

**Files:**
- Modify: `claw_v2/types.py`
- Create: `claw_v2/artifacts.py`
- Modify: `claw_v2/brain.py`
- Modify: `claw_v2/kairos.py`
- Modify: `claw_v2/pipeline.py`
- Modify: `claw_v2/computer.py`
- Modify: `claw_v2/notebooklm.py`
- Create: `tests/test_artifacts.py`

- [ ] Add dataclasses for `PlanArtifact`, `ExecutionArtifact`, `VerificationArtifact`, `ApprovalArtifact`, `JobArtifact`.
- [ ] Persist artifacts in a consistent shape, not ad hoc dict payloads.
- [ ] Make approvals and verifications point to artifact IDs.
- [ ] Attach artifact lineage to `observe` events.

**Acceptance criteria:**
- [ ] A single request can be replayed from persisted artifacts.
- [ ] Brain, pipeline, Kairos, and computer-use emit the same artifact envelope.

### P0.2 Durable job system

**Goal:** Treat long tasks as resumable jobs instead of one-shot flows.

**Files:**
- Create: `claw_v2/jobs.py`
- Modify: `claw_v2/pipeline.py`
- Modify: `claw_v2/notebooklm.py`
- Modify: `claw_v2/main.py`
- Modify: `claw_v2/daemon.py`
- Modify: `claw_v2/bot.py`
- Create: `tests/test_jobs.py`

- [ ] Add `JobService` with states: `queued`, `running`, `waiting_approval`, `retrying`, `completed`, `failed`, `cancelled`.
- [ ] Move pipeline and NotebookLM background work onto `JobService`.
- [ ] Persist job checkpoints under a stable state root.
- [ ] Add retry policies with idempotent resume semantics.
- [ ] Add `/jobs`, `/job_status <id>`, `/job_cancel <id>` commands.

**Acceptance criteria:**
- [ ] Restarting the daemon does not lose active pipeline/NLM jobs.
- [ ] Jobs can be resumed without repeating already committed side effects.

### P0.3 Trace-first observability

**Goal:** Make debugging and evaluation possible without reading logs manually.

**Files:**
- Modify: `claw_v2/observe.py`
- Modify: `claw_v2/hooks.py`
- Modify: `claw_v2/brain.py`
- Modify: `claw_v2/coordinator.py`
- Modify: `claw_v2/kairos.py`
- Create: `tests/test_observe_traces.py`

- [ ] Add `trace_id`, `span_id`, `parent_span_id`, `artifact_id`, `job_id` to observe payloads.
- [ ] Add trace helpers so nested flows share a lineage.
- [ ] Record model routing, tool usage, approvals, retries, and fallbacks as spans.
- [ ] Add a simple trace replay reader.

**Acceptance criteria:**
- [ ] You can answer “why did the agent do this?” from trace data alone.
- [ ] Tool failures and model fallbacks are linked to the parent request.

### P0.4 Single runtime registry for agent capabilities

**Goal:** One source of truth for agent identity, model, tools, skills, budget, and policy.

**Files:**
- Modify: `claw_v2/agents.py`
- Modify: `claw_v2/heartbeat.py`
- Modify: `claw_v2/coordinator.py`
- Modify: `claw_v2/main.py`
- Modify: `claw_v2/bus.py`
- Create: `tests/test_agent_registry_runtime.py`

- [ ] Extend sub-agent registry to include domains, tools, risk policy, budget, preferred lanes, and SLA.
- [ ] Remove remaining hardcoded `KNOWN_AGENTS` assumptions where possible.
- [ ] Dispatch coordinator work by capability tags, not just assigned names.
- [ ] Make heartbeat and ecosystem health read from the same registry.

**Acceptance criteria:**
- [ ] Adding a new agent only requires a definition entry plus skills, not code branches in multiple modules.

---

## Phase 1 — Make The Agent Measurable

### Outcome

Every meaningful behavior is evaluable. Prompt changes, provider shifts, and autonomy expansions are gated by measurable regressions.

### P1.1 Eval dataset and replay harness

**Goal:** Build a stable corpus of real tasks and outcomes.

**Files:**
- Create: `evals/agent_tasks.jsonl`
- Create: `claw_v2/evals.py`
- Create: `tests/test_evals.py`
- Modify: `claw_v2/memory.py`

- [ ] Define task record schema: input, context, expected behavior, risk level, accepted outputs, banned actions.
- [ ] Export real tasks from `messages`, `task_outcomes`, and traces into eval-ready format.
- [ ] Build replay harness that re-runs tasks against current prompts/models without external side effects.

**Acceptance criteria:**
- [ ] At least 25 high-value tasks across: bot chat, browse, computer-use, pipeline, and sub-agent dispatch.

### P1.2 Multi-axis grading

**Goal:** Evaluate more than “did it reply”.

**Files:**
- Create: `claw_v2/eval_graders.py`
- Modify: `claw_v2/evals.py`
- Create: `tests/test_eval_graders.py`

- [ ] Grade for factuality/goal completion.
- [ ] Grade for unnecessary tool use.
- [ ] Grade for risk-policy compliance.
- [ ] Grade for latency and cost envelope.
- [ ] Grade for handoff quality between planner/executor/verifier.

**Acceptance criteria:**
- [ ] Eval output shows per-lane and per-workflow scores.
- [ ] Failures produce actionable diffs, not just pass/fail.

### P1.3 Regression gates for prompts and routing

**Goal:** No silent degradation after prompt/model/routing changes.

**Files:**
- Modify: `claw_v2/main.py`
- Modify: `claw_v2/llm.py`
- Modify: `claw_v2/hooks.py`
- Create: `scripts/run_agent_evals.py`

- [ ] Add offline eval command for CI/local use.
- [ ] Block risky config changes when eval score drops beyond threshold.
- [ ] Produce a scorecard artifact for each run.

**Acceptance criteria:**
- [ ] A provider swap or prompt edit can be compared against a known baseline before rollout.

---

## Phase 2 — Make The Agent Smarter

### Outcome

Claw becomes capability-aware, memory-rich, and strategically autonomous instead of just feature-rich.

### P2.1 Layered memory

**Goal:** Separate short-term context, episodic history, semantic facts, and cross-agent memory.

**Files:**
- Modify: `claw_v2/memory.py`
- Modify: `claw_v2/dream.py`
- Modify: `claw_v2/brain.py`
- Create: `claw_v2/memory_layers.py`
- Create: `tests/test_memory_layers.py`

- [ ] Add explicit namespaces: `working`, `episodic`, `semantic`, `policy`, `shared`.
- [ ] Add provenance and freshness scoring per fact.
- [ ] Add conflict resolution that prefers trusted, fresh, and verified facts.
- [ ] Make `dream` consolidate by layer instead of bulk-searching generic facts.

**Acceptance criteria:**
- [ ] Contradictory facts can be explained and resolved, not just overwritten.

### P2.2 Capability-aware planner/executor/verifier loop

**Goal:** Use specialist agents intentionally.

**Files:**
- Modify: `claw_v2/coordinator.py`
- Modify: `claw_v2/kairos.py`
- Modify: `claw_v2/agents.py`
- Modify: `claw_v2/plan_gate.py`
- Create: `tests/test_capability_dispatch.py`

- [ ] Planner emits steps tagged by required capabilities.
- [ ] Coordinator maps capabilities to agents dynamically.
- [ ] Verifier chooses stricter models/rules for high-risk tasks.
- [ ] Kairos dispatches based on urgency + specialization, not fixed if/else rules.

**Acceptance criteria:**
- [ ] “Hex for code, Rook for ops, Alma for user-facing synthesis, Lux for content” becomes policy-backed behavior, not convention.

### P2.3 Adaptive routing by SLO

**Goal:** Replace lane-only routing with policy-based routing.

**Files:**
- Modify: `claw_v2/config.py`
- Modify: `claw_v2/llm.py`
- Create: `claw_v2/routing_policy.py`
- Create: `tests/test_routing_policy.py`

- [ ] Route using task class, risk, latency target, cost ceiling, tool need, and historical quality.
- [ ] Add provider health and fallback history to routing decisions.
- [ ] Persist routing decisions as traceable artifacts.

**Acceptance criteria:**
- [ ] Routing can explain why a provider/model was chosen.

---

## Phase 3 — Make The Agent Safer And More Autonomous

### Outcome

The agent can take on more real work without becoming brittle or unsafe.

### P3.1 Policy engine for external actions

**Goal:** Centralize autonomy rules.

**Files:**
- Create: `claw_v2/policy.py`
- Modify: `claw_v2/computer_gate.py`
- Modify: `claw_v2/approval.py`
- Modify: `claw_v2/browser.py`
- Modify: `claw_v2/computer.py`
- Create: `tests/test_policy_engine.py`

- [ ] Express policies by action, agent, domain, tool, and risk.
- [ ] Add semantic approval summaries for all writes.
- [ ] Add simulation mode for high-risk actions.
- [ ] Detect prompt-injection signals on fetched content and browser surfaces.

**Acceptance criteria:**
- [ ] External writes are governed by one engine, not scattered conditional logic.

### P3.2 Browser/computer action memory and recovery

**Goal:** Stop re-learning the same UI patterns.

**Files:**
- Modify: `claw_v2/browser.py`
- Modify: `claw_v2/computer.py`
- Modify: `claw_v2/memory.py`
- Create: `tests/test_ui_recovery.py`

- [ ] Store successful selectors, page anchors, and interaction recipes.
- [ ] Add fallback recovery strategies when UI changes.
- [ ] Learn from approval outcomes and failed actions.

**Acceptance criteria:**
- [ ] Repeated tasks on the same app/domain get faster and more reliable over time.

### P3.3 Human operator dashboard

**Goal:** Move beyond Telegram for deep operations.

**Files:**
- Create: `prototypes/ops-dashboard/`
- Create: `claw_v2/api.py`
- Modify: `claw_v2/main.py`

- [ ] Show jobs, traces, approvals, agent health, and costs.
- [ ] Inspect artifact lineage and replay failed runs.
- [ ] Support manual resume/cancel/retry and policy overrides.

**Acceptance criteria:**
- [ ] Common debugging and approval tasks no longer require reading raw logs or SQLite directly.

---

## Recommended Order

1. `P0.1` Typed artifacts
2. `P0.2` Durable jobs
3. `P0.3` Trace-first observability
4. `P0.4` Single runtime registry
5. `P1.1` Eval dataset and replay harness
6. `P1.2` Multi-axis grading
7. `P1.3` Regression gates
8. `P2.1` Layered memory
9. `P2.2` Capability-aware planner/executor/verifier
10. `P2.3` Adaptive routing by SLO
11. `P3.1` Policy engine
12. `P3.2` UI recovery memory
13. `P3.3` Operator dashboard

---

## What Not To Do Yet

- [ ] Do not add more Telegram commands before Phase 0.
- [ ] Do not expand autonomous write actions before `policy.py`.
- [ ] Do not add more sub-agents until capability dispatch is in place.
- [ ] Do not swap core providers aggressively before eval gates exist.

---

## Metrics To Track

- [ ] Task success rate by workflow
- [ ] Approval-required rate vs auto-safe rate
- [ ] Average retries per job
- [ ] Cost per successful task
- [ ] Tool calls per successful task
- [ ] Mean time to diagnose failed runs
- [ ] Regression score after prompt/model changes

---

## Definition Of “Next Level”

Claw reaches the next level when:

- [ ] It can run complex tasks durably across restarts.
- [ ] It can explain, replay, and evaluate its own behavior.
- [ ] It uses specialist agents intentionally and measurably.
- [ ] It improves safely because prompt and routing changes are regression-tested.
- [ ] It expands autonomy by policy, not by trust in happy paths.
