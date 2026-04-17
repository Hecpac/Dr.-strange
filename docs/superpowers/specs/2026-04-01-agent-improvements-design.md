# Agent System Improvements — Design Spec

**Date:** 2026-04-01
**Status:** Draft
**Scope:** Claw v2 agent ecosystem — infrastructure, skills, and consolidation

---

## Overview

The Claw v2 multi-agent system has 4 named agents (Hex, Rook, Alma, Lux) orchestrated via a shared runtime. An audit revealed three structural gaps:

1. **Skills imbalance:** Lux has 12 skills; Hex, Rook, and Alma have 1 each.
2. **No inter-agent communication:** Agents operate as silos. No mechanism for one agent to notify, request from, or escalate to another.
3. **Incomplete autonomy loop:** Kairos decides but cannot execute. Dream consolidates per-agent but not across agents. Coordinator dispatches generic workers, unaware of agent specialties. AGENTS.md registry is empty.

This spec addresses all three gaps in 3 layers, ordered by dependency: infrastructure first, skills second, consolidation third.

## Agent Model Registry

| Agent | Role | Model | Skills (current) | Skills (proposed) |
|-------|------|-------|-------------------|-------------------|
| Hex | Code engine | GPT-5.3 Codex | 1 (bug-triage) | 4 (+code-review, dependency-audit, refactor-plan) |
| Rook | Operations sentinel | Claude Sonnet 4.6 | 1 (health-audit) | 4 (+incident-response, log-analysis, cron-doctor) |
| Alma | Companion AI | Claude Opus 4.6 | 1 (daily-brief) | 4 (+pending-items, weekly-retro, context-bridge) |
| Lux | Creative strategist | GPT-5.4 | 12 | 12 (no changes) |

---

## Layer 1: Connect — Critical Infrastructure

### 1.1 Inter-Agent Bus (`claw_v2/bus.py`)

A message bus that allows agents to communicate asynchronously via typed messages persisted to disk.

#### Data Model

```python
@dataclass(slots=True)
class AgentMessage:
    id: str                          # uuid4
    from_agent: str                  # "rook"
    to_agent: str | None             # "hex" or None for broadcast
    intent: Literal["notify", "request", "escalate", "reply"]
    topic: str                       # "test_failure", "deploy_needed", "pr_ready"
    payload: dict[str, Any]
    priority: Literal["low", "normal", "urgent"]
    ttl_seconds: int = 3600          # default 1 hour
    correlation_id: str = ""         # for request/reply chains
    created_at: float = 0.0         # time.time()
    consumed_at: float | None = None
```

#### Storage

Messages are persisted as individual JSON files:

```
~/.claw/bus/
  inbox/
    hex/
      {message_id}.json
    rook/
      {message_id}.json
    alma/
      {message_id}.json
    lux/
      {message_id}.json
  broadcast/
    {message_id}.json
  archive/
    {message_id}.json    # consumed messages, kept 7 days
```

#### API

```python
class AgentBus:
    def __init__(self, bus_root: Path = Path.home() / ".claw" / "bus") -> None: ...

    def send(self, message: AgentMessage) -> str:
        """Persist message to recipient inbox. Returns message_id.
        Broadcasts use eager fan-out: send() copies the message into every agent's
        inbox at write time (one file per agent). The broadcast/ directory is not
        used at runtime — it only stores the original for audit.
        Escalations bypass inbox and emit 'bus_escalation' event for Kairos."""

    def receive(self, agent_name: str, *, max_messages: int = 20) -> list[AgentMessage]:
        """Consume messages from agent's inbox only. Moves consumed to archive.
        Skips expired messages (created_at + ttl_seconds < now). Returns newest first.
        Because broadcasts are eagerly fanned out by send(), receive() does not
        need to touch broadcast/ — each agent already has their own copy."""

    def reply(self, original: AgentMessage, *, content: dict, from_agent: str) -> str:
        """Send a reply linked to the original via correlation_id."""

    def pending_count(self, agent_name: str) -> int:
        """Count unconsumed messages in inbox. Used by ecosystem health metrics."""

    def pending_urgent(self) -> list[AgentMessage]:
        """All unconsumed messages with priority=urgent across all inboxes.
        Used by Kairos _gather_context()."""

    def scan_expired_requests(self) -> list[AgentMessage]:
        """Scan all inboxes for intent=request messages past TTL without a matching reply.
        Returns expired requests. Called by Kairos on each tick to detect timeouts.
        Does NOT auto-archive — Kairos decides whether to escalate or retry."""

    def cleanup(self, max_age_days: int = 7) -> int:
        """Remove archived messages older than max_age_days. Returns count removed."""
```

#### Intent Semantics

| Intent | Behavior | Timeout |
|--------|----------|---------|
| `notify` | Fire-and-forget. Recipient decides whether to act. | Message expires at TTL, no consequence. |
| `request` | Expects a `reply` within TTL. If no reply → auto-escalate to Hector via Alma/Telegram. **Timeout scanner:** `AgentBus.scan_expired_requests()` is called by Kairos on each tick. It scans all inboxes for `intent=request` messages past TTL without a matching `reply` (by `correlation_id`). Expired requests emit `bus_request_timeout` event and Kairos handles escalation via `escalate_to_human`. | Default TTL: 1 hour. |
| `escalate` | Bypasses inbox. Emits `bus_escalation` event immediately. Kairos picks it up on next tick (or wakes early if urgent). | Immediate processing. |
| `reply` | Links to original message via `correlation_id`. Delivered to original sender's inbox. | No timeout. |

#### Integration Points

- **Heartbeat:** Each agent calls `bus.receive()` at the start of every heartbeat cycle.
- **Kairos:** Reads `bus.pending_urgent()` in `_gather_context()`. Escalations trigger immediate tick.
- **Observe:** Bus emits events: `bus_message_sent`, `bus_message_consumed`, `bus_escalation`, `bus_request_timeout`.

---

### 1.2 Kairos Executable Actions

Current state: `kairos.py:_execute()` (line 195) only logs decisions. The change makes Kairos capable of real actions.

#### Action Dispatch Table

```python
ACTION_HANDLERS: dict[str, Callable] = {
    "notify_user":        _handle_notify_user,        # Tier 1
    "dispatch_to_agent":  _handle_dispatch_to_agent,   # Tier 1
    "approve_pending":    _handle_approve_pending,      # Tier 2
    "run_skill":          _handle_run_skill,            # Tier 2
    "pause_agent":        _handle_pause_agent,          # Tier 2
    "escalate_to_human":  _handle_escalate_to_human,    # Tier 1
}
```

#### Action Definitions

**`notify_user` (Tier 1 — just do it)**
- Sends message to Hector via Alma's Telegram channel.
- Input from decision: `detail` contains the message text.
- Falls back to logging if Telegram is unavailable.

**`dispatch_to_agent` (Tier 1 — just do it)**
- Publishes an `AgentMessage` to the bus.
- Input from decision: `detail` is JSON with `{to_agent, topic, payload}`.
- Validates that `to_agent` exists in the registry.

**`approve_pending` (Tier 2 — do it, log it)**
- Auto-approves a pending approval if risk_level is "low" and the action is Tier 1/2.
- Input from decision: `detail` contains `{approval_id}`.
- Emits `kairos_auto_approved` event for audit trail.
- Refuses to auto-approve Tier 3 actions (deploy, push, send external message, delete, spend money).

**`run_skill` (Tier 2 — do it, log it)**
- Executes a named skill for a named agent.
- Input from decision: `detail` is JSON with `{agent, skill}`.
- Validates skill exists for that agent.
- Runs via the existing skill execution path (not a new code path).

**`pause_agent` (Tier 2 — do it, log it)**
- Pauses an agent that is burning budget or stuck in a loop.
- Input from decision: `detail` contains `{agent_name, reason}`.
- Emits `agent_paused` event. Notifies Hector via `notify_user`.

**`escalate_to_human` (Tier 1 — just do it)**
- Sends an urgent Telegram message with full context.
- Input from decision: `detail` contains the escalation message.
- Marks the related bus message (if any) as escalated.

#### Enhanced Context Gathering

`_gather_context()` adds three new data sources:

```python
# 1. Urgent bus messages
urgent = self.bus.pending_urgent()
if urgent:
    parts.append(f"Urgent bus messages: {len(urgent)}")
    for msg in urgent[:3]:
        parts.append(f"  [{msg.from_agent}→{msg.to_agent}] {msg.topic}: {str(msg.payload)[:100]}")

# 2. Cost per agent today
for name, cost in self.observe.cost_per_agent_today().items():
    parts.append(f"  {name}: ${cost:.2f}")

# 3. Last successful action per agent
for name, info in snapshot.agents.items():
    last = info.get("last_success_at", "never")
    parts.append(f"  {name}: last success {last}")
```

#### New Dependencies

`KairosService.__init__()` receives two new parameters:
- `bus: AgentBus` — for dispatch_to_agent and reading urgent messages
- `approvals: ApprovalManager` — for approve_pending

---

### 1.3 Agent Registry (Live AGENTS.md)

The file `claw_v2/AGENTS.md` auto-updates on every system heartbeat.

#### Update Mechanism

A new function `update_agent_registry()` is called inside `HeartbeatService.emit()`, **not** `collect()`. This is important: `collect()` is a pure snapshot read used by multiple callers (Kairos `_gather_context()`, tests, telemetry). Putting file writes in `collect()` would introduce side effects in every read path and produce noisy `agent_registry_updated` events. The write belongs in `emit()` which is the scheduled heartbeat entrypoint (`heartbeat.py:58`) called by the cron scheduler:

```python
def update_agent_registry(snapshot: HeartbeatSnapshot, registry_path: Path) -> None:
    """Write current agent states to AGENTS.md as a markdown table."""
    header = "| Agent | Model | Status | Last Action | Last Metric | Cost Today | Health |\n"
    separator = "|-------|-------|--------|-------------|-------------|------------|--------|\n"
    rows = []
    for name, info in sorted(snapshot.agents.items()):
        status = "paused" if info.get("paused") else "active"
        last_action = info.get("last_action", "-")
        last_metric = info.get("last_metric", "-")
        cost = f"${info.get('cost_today', 0):.2f}"
        health = _compute_health(info)
        model = info.get("model", "-")
        rows.append(f"| {name} | {model} | {status} | {last_action} | {last_metric} | {cost} | {health} |")
    content = f"# Agent Registry\n\nAuto-updated every heartbeat.\n\n{header}{separator}" + "\n".join(rows) + "\n"
    registry_path.write_text(content, encoding="utf-8")
```

Health is computed as:
- `OK` — active, no errors, cost < 80% budget
- `WARN:budget` — cost > 80% daily budget
- `WARN:silent` — no action in >24 hours
- `WARN:errors` — last skill/heartbeat had errors
- `CRITICAL` — paused by Kairos or multiple consecutive errors

The function is called inside `emit()` after the snapshot is created and the heartbeat event is emitted:

```python
def emit(self) -> HeartbeatSnapshot:
    snapshot = self.collect()
    if self.observe is not None:
        self.observe.emit("heartbeat", payload=asdict(snapshot))
    update_agent_registry(snapshot, self.registry_path)  # NEW: write after emit
    if self.observe is not None:
        self.observe.emit("agent_registry_updated")       # NEW: separate event
    return snapshot
```

This keeps `collect()` pure and ensures the registry write happens exactly once per scheduled heartbeat, not on every Kairos tick or test call.

---

## Layer 2: High-Impact Skills

### 2.1 Hex Skills (3 new)

#### `code-review` (agents/hex/skills/code-review/SKILL.md)

**Purpose:** Review PRs and diffs for correctness, security, performance, and style.

**Trigger:** Bus message with topic `pr_ready`, or manual request.

**Inputs:**
- Diff content or PR reference (repo + PR number)
- Project conventions (from CLAUDE.md or .editorconfig)
- Test coverage data if available

**Process:**
1. Parse diff into file-level change sets
2. Security scan: check for OWASP top 10 patterns (injection, XSS, auth bypass, secrets in code, insecure deserialization)
3. Logic analysis: detect potential null refs, unhandled errors, race conditions, off-by-one, boundary issues
4. Performance: flag N+1 queries, unbounded loops, missing indexes, unnecessary allocations
5. Style: check against project conventions (naming, imports, structure)
6. Test coverage: identify changed logic paths without corresponding test changes

**Output format:**
```
## Code Review: {PR title or diff summary}

### Findings

| # | File:Line | Category | Severity | Finding | Suggested Fix |
|---|-----------|----------|----------|---------|---------------|

Categories: SECURITY, LOGIC, PERFORMANCE, STYLE, TESTS
Severity: MUST-FIX (blocks merge), SHOULD-FIX (should address), NIT (optional)

### Summary
- MUST-FIX: {count}
- SHOULD-FIX: {count}
- NIT: {count}
- Recommendation: {APPROVE | REQUEST_CHANGES | BLOCK}
```

**Done criteria:**
- Every finding has exact file:line reference
- Every finding has a concrete suggested fix (not just "fix this")
- 0 false positives in SECURITY category (verify each pattern against context)
- MUST-FIX count directly determines recommendation

---

#### `dependency-audit` (agents/hex/skills/dependency-audit/SKILL.md)

**Purpose:** Audit project dependencies for security vulnerabilities, staleness, license issues, and unused packages.

**Trigger:** Weekly cron, or bus message with topic `security_alert`.

**Inputs:**
- Dependency files: requirements.txt, pyproject.toml, package.json, package-lock.json (whichever exist)
- Project source files (for unused detection via import grep)

**Process:**
1. Parse all dependency files into a unified list: `{name, version, source_file}`
2. CVE check: cross-reference each dependency against known vulnerability databases (pip-audit output, npm audit output, or OSV.dev API)
3. Freshness: compare installed version vs latest stable. Flag if >2 major versions behind or >1 year stale.
4. License scan: identify license of each dep. Flag GPL/AGPL in proprietary projects, or any unknown license.
5. Unused detection: grep project source for import/require of each dependency. Flag deps with 0 import matches (exclude dev dependencies from this check).
6. Pin analysis: flag unpinned deps (`>=` without upper bound) that could break on upgrade.

**Output format:**
```
## Dependency Audit: {project name}

### Summary
- Total dependencies: {count}
- Vulnerable: {count} ({critical}/{high}/{medium}/{low})
- Stale (>1yr): {count}
- Unused: {count}
- License issues: {count}

### Findings

| Package | Version | Status | Detail | Action | Effort |
|---------|---------|--------|--------|--------|--------|

Status: VULN-CRITICAL, VULN-HIGH, VULN-MEDIUM, STALE, UNUSED, LICENSE, PIN, OK
Effort: trivial (version bump), moderate (API changes), significant (major rewrite)
```

**Done criteria:**
- 0 VULN-CRITICAL or VULN-HIGH without explicit flag
- Every UNUSED finding verified with grep evidence (actual search command and 0 results)
- Every VULN has CVE ID and affected version range
- Upgrade path specified for every non-OK dependency

---

#### `refactor-plan` (agents/hex/skills/refactor-plan/SKILL.md)

**Purpose:** Identify systemic code smells and produce an ordered plan of atomic refactoring steps.

**Trigger:** Manual, or auto-triggered when code-review finds >3 SHOULD-FIX findings of the same pattern in a single PR.

**Inputs:**
- Codebase root or specific directories to analyze
- Code review findings (if triggered from code-review)
- Test suite location and run command

**Process:**
1. Smell detection: scan for duplication (>20 lines repeated), god files (>500 lines), circular imports, deep nesting (>4 levels), long parameter lists (>5 params)
2. Cluster: group related smells into refactoring themes (e.g., "extract service X", "split module Y")
3. Dependency analysis: for each theme, identify which files/functions depend on the code being changed
4. Order: topological sort — changes with no downstream dependents go first
5. Atomicity check: verify each step can be a single commit that passes all tests

**Output format:**
```
## Refactor Plan: {theme or scope}

### Smells Detected
| # | Type | Location | Impact |

### Plan (execute in order)

#### Step 1: {description}
- Files: {list}
- Change: {what to do, specifically}
- Risk: low|medium|high
- Tests: {which tests cover this, or "needs new test"}
- Commit message: {suggested}

#### Step 2: ...

### Dependencies
{Step N must complete before Step M because...}

### Estimated Total Effort
{count} steps, {risk assessment}
```

**Done criteria:**
- Each step is independently committable (tests pass after each step)
- No step depends on a later step
- Risk assessment per step with mitigation if medium/high
- If a step needs a new test, the test is specified (not just "add test")

---

### 2.2 Rook Skills (3 new)

#### `incident-response` (agents/rook/skills/incident-response/SKILL.md)

**Purpose:** Structured incident response when a critical issue is detected.

**Trigger:** Bus message with priority=urgent, or health-audit finding with severity=CRITICAL.

**Inputs:**
- Alert source (health-audit finding, bus escalation, or manual)
- Available log sources and their paths
- Recent deploy/change history (git log last 24h)
- Current service status

**Process:**
1. Evidence collection (parallel):
   - Last 30 minutes of relevant logs
   - Current service health (all endpoints)
   - Last 3 deploys/commits
   - Active cron jobs and their status
2. Timeline construction: order events leading to the incident
3. Severity classification:
   - SEV1: user-facing service down, data loss risk → notify Hector immediately
   - SEV2: degraded service, partial functionality lost → notify within 15 min
   - SEV3: non-critical component failing, no user impact → include in next heartbeat
4. Root cause hypothesis: at least 1 hypothesis with supporting evidence
5. Auto-execute Tier 1 mitigations: restart failed cron, clear stale locks, retry failed requests
6. Generate runbook for Tier 2/3 actions requiring approval

**Output format:**
```
## Incident Report

**Severity:** SEV{1|2|3}
**Status:** {investigating|mitigating|resolved}
**Detected:** {timestamp}
**Duration:** {ongoing or resolved at timestamp}

### Timeline
- {timestamp}: {event}

### Root Cause
**Hypothesis:** {description}
**Evidence:** {supporting data}
**Confidence:** {low|medium|high}

### Actions Taken (Tier 1 — auto-executed)
- {action}: {result}

### Actions Pending (require approval)
- {action}: {why needed} — {risk level}

### Notification
{who was notified and when}
```

**Done criteria:**
- Timeline has at least 3 data points from real logs/events (not hypothesized)
- At least 1 root cause hypothesis with evidence
- All Tier 1 mitigations attempted within 2 minutes of detection
- SEV1 triggers immediate Telegram notification to Hector (via bus → Alma)
- Actions pending have clear risk labels

---

#### `log-analysis` (agents/rook/skills/log-analysis/SKILL.md)

**Purpose:** Deep dive into logs to identify anomalous patterns and correlate with system events.

**Trigger:** Manual, or called by incident-response for deeper investigation.

**Inputs:**
- Log file paths or log source identifiers
- Time window to analyze (default: last 6 hours)
- Context: what prompted the analysis (incident, routine check, specific question)

**Process:**
1. Ingest: read last N lines within time window (cap at 10,000 lines to stay within context)
2. Pattern clustering: group log lines by regex pattern (strip timestamps, IDs, and variable data). Count occurrences per pattern.
3. Anomaly detection:
   - New patterns: patterns that appear for the first time in this window
   - Frequency spikes: patterns with >3x their baseline frequency
   - Error escalation: warning→error→critical progression on same component
   - Gap detection: expected periodic patterns that stopped appearing
4. Correlation: match anomalous patterns against recent events (deploys, cron runs, config changes, bus messages)
5. Rank by impact: patterns affecting more components or users rank higher

**Output format:**
```
## Log Analysis: {source} ({time window})

### Summary
- Lines analyzed: {count}
- Unique patterns: {count}
- Anomalies found: {count}

### Top Anomalies

#### 1. {pattern description}
- **Type:** new_pattern | frequency_spike | error_escalation | gap
- **Frequency:** {count} occurrences (baseline: {count})
- **First seen:** {timestamp}
- **Example:** `{actual log line}`
- **Correlation:** {related event or "none found"}
- **Impact:** {what this affects}

#### 2. ...
```

**Done criteria:**
- Every anomaly has a real log line as example (copy-pasted, not synthesized)
- Frequency counts are exact (from actual grep/count)
- First-seen timestamp is accurate
- At most 5 anomalies reported (top 5 by impact, not exhaustive)

---

#### `cron-doctor` (agents/rook/skills/cron-doctor/SKILL.md)

**Purpose:** Diagnose why a cron job is failing or was missed, and propose a fix.

**Trigger:** Heartbeat detects cron in error state or cron missed its schedule.

**Inputs:**
- Cron job name and schedule (from CRON.md)
- Cron execution history (last 10 runs: timestamps, exit codes, durations)
- Current CRON.md configuration
- System resource state at failure times (if available)

**Process:**
1. History analysis: plot success/failure pattern across last 10 runs
2. Failure pattern detection:
   - **Time-correlated:** always fails at the same hour → resource contention or scheduled conflict
   - **Sequence-correlated:** fails after another specific cron → dependency or resource leak
   - **Duration-correlated:** fails when runtime exceeds threshold → timeout
   - **Random:** no pattern → likely transient (network, API rate limit)
3. Dependency check: does this cron depend on a service, API, or file that may be unavailable?
4. Conflict check: are two crons scheduled to overlap that compete for the same resource?
5. Fix proposal: specific change to CRON.md, code, or infrastructure

**Output format:**
```
## Cron Doctor: {cron name}

**Schedule:** {cron expression}
**Status:** {failing|missed|intermittent}
**Pattern:** {time|sequence|duration|random}

### History
| Run | Timestamp | Exit Code | Duration | Status |
|-----|-----------|-----------|----------|--------|

### Diagnosis
**Pattern:** {description with evidence from >= 2 occurrences}
**Root cause:** {specific cause}
**Confidence:** {low|medium|high}

### Recommended Fix
- **Change:** {what to change}
- **Where:** {file and line, or CRON.md entry}
- **Risk:** {low|medium}
- **Verification:** {how to confirm the fix works}
```

**Done criteria:**
- Pattern identified with evidence from at least 2 failure occurrences
- Fix is specific (not "investigate further")
- If conflict detected, both conflicting crons are named
- Verification step is executable (a command or check, not "monitor")

---

### 2.3 Alma Skills (3 new)

#### `pending-items` (agents/alma/skills/pending-items/SKILL.md)

**Purpose:** Track and surface all open items across Hector's communication channels and agent activity.

**Trigger:** Daily after daily-brief completes, or manual.

**Inputs:**
- Recent Telegram messages (last 48h, unresolved threads)
- Bus messages directed to Alma or broadcast (last 48h)
- Active reminders in memory
- Calendar items with action items
- Recent agent outputs that mention Hector or need his input

**Process:**
1. Scan all sources for items that: have a question directed at Hector, mention a deadline, were promised by Hector to someone, are blocked waiting on Hector's decision
2. Deduplicate: same item mentioned in Telegram and bus → merge into one
3. Enrich: add context (who asked, when, what conversation, what the consequence of inaction is)
4. Prioritize: urgent (deadline today/overdue) > time-sensitive (deadline this week) > normal > low (no deadline, nice-to-have)

**Output format:**
```
## Pendientes — {date}

### Urgente (hoy/vencido)
1. {item} — {quien lo pidio, cuando} — {accion sugerida}

### Esta semana
1. {item} — {contexto} — {deadline}

### Sin deadline
1. {item} — {contexto}

**Total:** {count} items ({urgent} urgentes)
```

**Done criteria:**
- 0 duplicate items
- Every item has a verifiable source (message ID, calendar event, bus message ID)
- Urgent items have specific suggested action
- Output in Spanish (Alma's natural language with Hector)

---

#### `weekly-retro` (agents/alma/skills/weekly-retro/SKILL.md)

**Purpose:** Weekly retrospective based on real data from the agent ecosystem.

**Trigger:** Cron every Sunday at 10:00 AM CT.

**Inputs:**
- Git log for the week (all repos)
- Bus message history for the week
- Agent registry snapshots (daily)
- Published content (Lux outputs)
- Incident reports (Rook outputs)
- Daily briefs from the week (Alma outputs)

**Process:**
1. Achievements: extract from git log (PRs merged, features shipped), Lux (content published), Rook (incidents resolved)
2. Carryover: items from last week's retro marked "next week" that are still open
3. Pattern detection: recurring blockers, recurring task types, agent that needed most manual intervention
4. Insight: one non-obvious observation (e.g., "most PRs happen on Tuesday", "Lux cost spikes correlate with keyword research runs")
5. Suggestion: one actionable change for next week

**Output format:**
```
## Retro Semanal — {week range}

### Logrado
- {achievement with evidence}

### Pendiente arrastrado
- {item} — {originally due} — {blocker}

### Patron detectado
{description with data}

### Sugerencia para esta semana
{specific, actionable suggestion}
```

**Done criteria:**
- Achievements sourced from real data (git SHA, publish date, incident ID)
- At most 1 pattern (the most significant, not a laundry list)
- Suggestion is specific and actionable (not "be more productive")
- Under 300 words total

---

#### `context-bridge` (agents/alma/skills/context-bridge/SKILL.md)

**Purpose:** Translate between Hector's personal context and the technical/marketing context of other agents.

**Trigger:** When Hector mentions something technical in Telegram that should be routed to another agent, or when an agent via bus requests personal context to make a decision.

**Inputs:**
- Source message or request (Telegram message or bus message)
- Hector's recent context: calendar, recent conversations, stated priorities, mood/energy if mentioned
- Target agent's domain and current state

**Process:**
1. Intent detection: what does the source message actually need? (information, action, decision, context)
2. Context assembly: gather relevant personal context without over-sharing
3. Translation: reframe in terms the target agent understands
   - For Hex: technical framing, repo/file references, priority level
   - For Rook: operational framing, urgency, affected services
   - For Lux: business framing, audience, timeline, brand voice
4. Privacy filter: strip personal details not relevant to the task (health, relationships, finances unless directly relevant)
5. Delivery: send as enriched bus message to target agent

**Output:** An `AgentMessage` via bus with:
- `intent: "notify"` or `"request"` depending on whether action is needed
- `topic: "context_bridge"`
- `payload: {original_request, enriched_context, suggested_action, privacy_note}`

**Done criteria:**
- Target agent can act without asking Hector for more context
- No personal details leaked beyond what's necessary for the task
- Original request intent is preserved (not reinterpreted)
- If Alma is unsure about privacy, she asks Hector before sending

---

## Layer 3: Consolidation

### 3.1 Cross-Agent Dream

Current state: The runtime has **one shared `MemoryStore`** (created at `main.py:97`) and **one `AutoDreamService`** (created at `main.py:280`). The `facts` table in `memory.py:20` has no `agent_name` column — all facts are global.

#### Prerequisite: Agent-Scoped Facts

Before cross-agent dream can work, the memory layer needs agent ownership:

1. **Add `agent_name TEXT NOT NULL DEFAULT 'system'` column to the `facts` table** (`claw_v2/memory.py`). Migrate existing facts to `agent_name='system'`.
2. **Scope `MemoryStore.store_fact()` and `search_facts()`** to accept an optional `agent_name` filter. When an agent stores a fact, it tags it with its name. When it searches, it sees its own facts + system facts.
3. **Create one `AutoDreamService` instance per agent** in `main.py`, each initialized with the agent's name and its import tags. The existing single instance becomes the `system` dreamer for shared facts.
4. **Update `EcosystemHealthService`** to query per-agent dream state for `dream_freshness` and `shared_memory_drift` metrics.

**Files added to the change plan:** `claw_v2/memory.py` (schema migration + scoped queries), `claw_v2/main.py` (per-agent dream instantiation).

#### Shared Memory Directory

```
~/.claw/shared-memory/
  hex_exports.jsonl       # one JSON object per line
  rook_exports.jsonl
  alma_exports.jsonl
  lux_exports.jsonl
```

Each line is a fact with metadata:
```json
{"key": "cron_seo_conflict", "value": "SEO audit cron conflicts with health-audit at 8AM Mondays", "source_agent": "rook", "confidence": 0.8, "timestamp": 1743500000, "tags": ["infra", "cron", "seo"]}
```

#### Tag Taxonomy

| Tag | Description | Example facts |
|-----|-------------|---------------|
| `code` | Code-related: bugs, patterns, architecture | "module X has recurring null ref bug" |
| `infra` | Infrastructure: services, deploys, resources | "disk usage spikes on backup days" |
| `cron` | Cron scheduling and execution | "SEO cron conflicts with health-audit" |
| `seo` | SEO-specific findings | "pachanodesign.com core web vitals degraded" |
| `marketing` | Marketing strategy and content | "LinkedIn posts perform best on Tuesdays" |
| `content` | Content creation and publishing | "newsletter open rate dropped 5% this month" |
| `personal` | Hector's personal context | "prefers morning meetings before 10AM" |
| `deploy` | Deployment and release | "last deploy broke sitemap generation" |
| `security` | Security findings | "dependency X has unpatched CVE" |

#### Import Matrix

Each agent imports facts from others based on tag relevance:

| Agent | Imports tags | Rationale |
|-------|-------------|-----------|
| Hex | `code`, `infra`, `deploy`, `security`, `cron` | Needs to know about bugs, infra state, and deploy issues |
| Rook | `code` (bug-related), `infra`, `cron`, `deploy`, `security` | Needs to know about code bugs affecting infra |
| Alma | All tags | Companion needs the full picture |
| Lux | `infra`, `seo`, `marketing`, `content`, `deploy` | Needs to know about availability and marketing data |

**Privacy rule:** Facts tagged `personal` are only importable by Alma. All other agents skip `personal`-tagged facts during import.

#### Changes to `dream.py`

Two new methods in `AutoDreamService`:

```python
def _export_shared(self, new_facts: list[dict]) -> int:
    """Post-consolidation: write new/updated facts to shared-memory exports.
    Only exports facts with confidence >= 0.6. Returns count exported."""

def _import_shared(self, agent_name: str, import_tags: list[str]) -> list[dict]:
    """Pre-orient: read exports from other agents, filtered by tags.
    Skips facts already present in this agent's memory (by key).
    Skips 'personal' tag unless agent_name == 'alma'.
    Returns list of imported facts for consolidation."""
```

Updated `run()` flow:
1. `_import_shared()` — read cross-agent facts
2. `_orient()` — read own facts + imported facts
3. `_gather_signal()` — extract from events
4. `_consolidate()` — merge all (LLM handles dedup naturally)
5. `_export_shared()` — write back new learnings
6. `_prune()` — enforce max_facts

---

### 3.2 Coordinator with Agent Awareness

Current state: `CoordinatorService` dispatches `WorkerTask` to the LLM router on generic lanes. It does not know that specialized agents exist.

#### Agent Capability Registry

```python
AGENT_CAPABILITIES: dict[str, dict] = {
    "hex":  {
        "domains": ["code", "debug", "review", "refactor", "dependencies"],
        "provider": "openai",
        "model": "gpt-5.3-codex",
        "skills": ["bug-triage", "code-review", "dependency-audit", "refactor-plan"],
    },
    "rook": {
        "domains": ["infra", "monitoring", "cron", "security", "logs", "incidents"],
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "skills": ["health-audit", "incident-response", "log-analysis", "cron-doctor"],
    },
    "alma": {
        "domains": ["personal", "communication", "context", "scheduling", "reminders"],
        "provider": "anthropic",
        "model": "claude-opus-4-6",
        "skills": ["daily-brief", "pending-items", "weekly-retro", "context-bridge"],
    },
    "lux":  {
        "domains": ["marketing", "seo", "content", "ads", "analytics", "social"],
        "provider": "openai",
        "model": "gpt-5.4",
        "skills": ["content-radar", "marketing-agent", "seo-aeo-audit", "google-ads-manager",
                   "meta-ads-manager", "linkedin-ads-manager", "keyword-intelligence",
                   "competitor-spy", "content-brief-generator", "campaign-reporter"],
    },
}
```

This registry is loaded from agent definitions at startup (reading each agent's SOUL.md and skills directory), not hardcoded. The above is the current expected state.

**Important:** Each entry includes `provider` alongside `model` because `LLMRouter.ask()` resolves provider from the lane config when none is passed (`claw_v2/llm.py:52`). Without explicit provider, a Hex task assigned to the `research` lane would route to Anthropic with a GPT model string. The `SubAgentDefinition` in `claw_v2/agents.py` already stores provider — the registry must read from there rather than parsing SOUL.md model names, since the SOUL text uses display names ("GPT-5.3 Codex") that don't map to API model IDs.

#### Changes to `_synthesize()`

The synthesis prompt now includes agent capabilities:

```python
agent_context = "\n".join(
    f"- {name}: domains={caps['domains']}, model={caps['model']}, skills={caps['skills']}"
    for name, caps in self.agent_registry.items()
)

prompt = (
    "You are a coordinator agent. Synthesize the research findings below "
    "into a clear, actionable plan.\n\n"
    f"## Objective\n{objective}\n\n"
    f"## Available Agents\n{agent_context}\n\n"
    f"## Research Findings\n{findings}\n\n"
    "Output a structured plan with numbered steps. "
    "For each step, assign it to the most appropriate agent based on their domains and skills. "
    "Use the format: **Step N [agent_name]:** description"
)
```

#### Changes to `WorkerTask`

Add optional `assigned_agent` field:

```python
@dataclass(slots=True)
class WorkerTask:
    name: str
    instruction: str
    lane: str = "research"
    assigned_agent: str | None = None  # NEW: if set, use this agent's model and context
```

#### Changes to `_execute_worker()`

When `assigned_agent` is set, the router uses that agent's model and injects the agent's SOUL.md as system context:

```python
def _execute_worker(self, task: WorkerTask) -> WorkerResult:
    start = time.time()
    try:
        kwargs = {"lane": task.lane, "evidence_pack": {"coordinator_task": task.name}}
        if task.assigned_agent and task.assigned_agent in self.agent_registry:
            agent = self.agent_registry[task.assigned_agent]
            kwargs["provider"] = agent["provider"]   # must pass provider explicitly
            kwargs["model"] = agent["model"]          # API model ID, not display name
            kwargs["system_prompt"] = agent.get("soul_text", "")  # LLMRouter uses system_prompt, not system_context
        response = self.router.ask(task.instruction, **kwargs)
        return WorkerResult(task_name=task.name, content=response.content, duration_seconds=time.time() - start)
    except Exception as exc:
        return WorkerResult(task_name=task.name, content="", duration_seconds=time.time() - start, error=str(exc))
```

**Note:** The registry must store API model IDs (e.g., `"gpt-5.3-codex"`) not SOUL.md display names (e.g., `"GPT-5.3 Codex"`). The `SubAgentDefinition` dataclass in `claw_v2/agents.py` already stores `provider` and `model` as API-ready strings — use those directly.

#### New Dependencies

`CoordinatorService.__init__()` receives:
- `agent_registry: dict` — loaded from agent definitions at startup

---

### 3.3 Ecosystem Health Metrics (`claw_v2/ecosystem.py`)

A new module that computes system-wide health metrics from bus, heartbeat, dream, and agent data.

#### Metrics

| Metric | Source | Computation | Thresholds |
|--------|--------|-------------|------------|
| `bus_lag` | AgentBus | Count messages in all inboxes older than 30 minutes | >3 WARN, >10 CRITICAL |
| `cross_agent_latency` | AgentBus archive | Average time between `request` send and `reply` consumed (last 24h) | >5min WARN, >15min CRITICAL |
| `dream_freshness` | AutoDreamService state per agent | Hours since last dream per agent | >48h WARN, >72h CRITICAL |
| `skill_success_rate` | ObserveStream events | % of skill executions without error in last 7 days | <80% WARN, <50% CRITICAL |
| `cost_distribution` | ObserveStream cost tracking | Each agent's spend today vs daily budget | >80% WARN, >95% CRITICAL |
| `agent_silence` | Agent Registry | Time since last action per agent | >24h WARN, >48h CRITICAL |
| `shared_memory_drift` | Shared memory exports | Facts exported by agent A but not imported by agent B after 2+ dream cycles | >5 facts WARN |

#### API

```python
@dataclass(slots=True)
class EcosystemMetric:
    name: str
    value: float
    status: Literal["OK", "WARN", "CRITICAL"]
    detail: str

@dataclass(slots=True)
class EcosystemHealth:
    timestamp: float
    metrics: list[EcosystemMetric]
    overall: Literal["OK", "WARN", "CRITICAL"]  # worst of all metrics

class EcosystemHealthService:
    def __init__(self, *, bus: AgentBus, observe: ObserveStream,
                 dream_services: dict[str, AutoDreamService],
                 heartbeat: HeartbeatService) -> None: ...

    def collect(self) -> EcosystemHealth:
        """Compute all metrics and return ecosystem health snapshot."""

    def write_dashboard(self, path: Path = Path.home() / ".claw" / "ecosystem-health.md") -> None:
        """Write human-readable dashboard file. Called by system heartbeat."""
```

#### Dashboard Output (`~/.claw/ecosystem-health.md`)

```markdown
# Ecosystem Health — {timestamp}

**Overall: {OK|WARN|CRITICAL}**

| Metric | Value | Status | Detail |
|--------|-------|--------|--------|
| bus_lag | 2 | OK | 2 messages pending, oldest 12min |
| cross_agent_latency | 3.2min | OK | avg over 8 request/reply pairs |
| dream_freshness | hex:6h rook:12h alma:3h lux:18h | OK | all within 48h |
| skill_success_rate | 94% | OK | 32/34 successful last 7d |
| cost_distribution | hex:$0.12 rook:$0.03 alma:$0.45 lux:$1.20 | OK | all under 80% |
| agent_silence | all active <12h | OK | - |
| shared_memory_drift | 0 | OK | all exports consumed |
```

#### Integration

- Kairos reads `EcosystemHealth.overall` in `_gather_context()`. If CRITICAL, Kairos prioritizes ecosystem issues over individual agent actions.
- System heartbeat calls `write_dashboard()` after updating Agent Registry.
- Rook's health-audit skill includes ecosystem health as a data source.

---

## Files Changed Summary

| Layer | File | Action | Description |
|-------|------|--------|-------------|
| 1 | `claw_v2/bus.py` | **New** | Inter-agent message bus |
| 1 | `claw_v2/kairos.py` | Modify | Add action dispatch table, enhanced context, bus/approval deps |
| 1 | `claw_v2/heartbeat.py` | Modify | Add `update_agent_registry()` call inside `emit()`, after snapshot creation |
| 1 | `claw_v2/AGENTS.md` | Modify | Auto-populated by heartbeat |
| 2 | `agents/hex/skills/code-review/SKILL.md` | **New** | PR and diff review skill |
| 2 | `agents/hex/skills/dependency-audit/SKILL.md` | **New** | Dependency security and freshness audit |
| 2 | `agents/hex/skills/refactor-plan/SKILL.md` | **New** | Systemic refactoring planner |
| 2 | `agents/rook/skills/incident-response/SKILL.md` | **New** | Structured incident response |
| 2 | `agents/rook/skills/log-analysis/SKILL.md` | **New** | Deep log pattern analysis |
| 2 | `agents/rook/skills/cron-doctor/SKILL.md` | **New** | Cron failure diagnosis |
| 2 | `agents/alma/skills/pending-items/SKILL.md` | **New** | Open items tracker |
| 2 | `agents/alma/skills/weekly-retro/SKILL.md` | **New** | Data-driven weekly retrospective |
| 2 | `agents/alma/skills/context-bridge/SKILL.md` | **New** | Cross-agent context translation |
| 3 | `claw_v2/memory.py` | Modify | Add `agent_name` column to facts table, scoped queries, schema migration |
| 3 | `claw_v2/main.py` | Modify | Per-agent `AutoDreamService` instantiation, pass agent names and import tags |
| 3 | `claw_v2/dream.py` | Modify | Add `_export_shared()`, `_import_shared()`, updated `run()` flow, agent_name awareness |
| 3 | `claw_v2/coordinator.py` | Modify | Add agent registry with provider field, aware synthesis, agent-assigned workers |
| 3 | `claw_v2/ecosystem.py` | **New** | Ecosystem health metrics and dashboard |

**New files:** 11 (bus.py Layer 1, 9 SKILL.md files Layer 2, ecosystem.py Layer 3)
**Modified files:** 7 (kairos.py, heartbeat.py, AGENTS.md, memory.py, main.py, dream.py, coordinator.py)
**New methods in existing modules:** `ObserveStream.cost_per_agent_today()` (returns dict[str, float]), `_compute_health(info: dict) -> str` helper in heartbeat.py, `AgentBus.scan_expired_requests()` timeout scanner

## Dependencies Between Layers

```
Layer 1 (bus.py, kairos executable, registry)
  ↓
Layer 2 (skills use bus for triggers, registry for context)
  ↓
Layer 3 (dream uses shared-memory, coordinator uses registry, ecosystem uses bus+heartbeat+dream)
```

Layers 1 and 2 can be built in parallel (skills don't strictly require bus to function — bus just enables auto-triggers). Layer 3 depends on both 1 and 2 being complete.

## Update to Agent SOUL.md Files

Each agent's SOUL.md needs a new section documenting bus topics they publish and subscribe to:

**Hex SOUL.md addition:**
- Publishes: `pr_ready`, `tests_fixed`, `dependency_alert`
- Subscribes: `test_failure`, `deploy_needed`, `context_bridge`, `security_alert`

**Rook SOUL.md addition:**
- Publishes: `health_critical`, `cron_failure`, `security_alert`, `deploy_complete`
- Subscribes: `pr_ready` (to monitor deploys), `context_bridge`

**Alma SOUL.md addition:**
- Publishes: `user_request`, `context_bridge`, `reminder_due`
- Subscribes: all topics (companion sees everything)

**Lux SOUL.md addition:**
- Publishes: `content_published`, `seo_alert`, `draft_ready`
- Subscribes: `deploy_complete`, `context_bridge`, `security_alert`

## Lux Model Update

Lux's SOUL.md model line changes from:
```
**Model:** Gemini 3 Pro
```
to:
```
**Model:** GPT-5.4
```

All references to Gemini 3 Pro in Lux's configuration are updated to GPT-5.4.
