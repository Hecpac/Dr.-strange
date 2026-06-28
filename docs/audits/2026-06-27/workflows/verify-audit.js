export const meta = {
  name: 'verify-audit-2026-06-27',
  description: 'Read-only verification of the claw_v2 full audit against current HEAD (610bfea); audit ran at 6eb6ab9, 59 commits stale',
  phases: [
    { title: 'Verify', detail: '15 read-only Explore agents verify each audit finding vs the current working tree, with drift attribution' },
  ],
}

const BASE = '6eb6ab9'   // audited HEAD
const HEAD = '610bfea'   // current HEAD; agents read the live working tree

const PREAMBLE = [
  'You verify findings from a STALE code audit against the CURRENT working tree. STRICTLY READ-ONLY.',
  '',
  'CONTEXT:',
  '- The audit ran at git commit ' + BASE + ' ("audited HEAD").',
  '- Current HEAD is ' + HEAD + ' — 59 commits AHEAD of the audit, plus some uncommitted working-tree mods.',
  '- Verify against what Read/Grep see NOW (current working tree), NOT the audited commit. Never checkout/stash anything.',
  '',
  'HARD RULES:',
  '- READ-ONLY: use Read/Grep/Glob, read-only git (log/show/diff/grep/rev-list), and ONLY the specific shell commands your task explicitly lists. NEVER Edit/Write, never mutate files/git/db, never git checkout/stash/add/commit. NEVER run the full test suite (it can restart the live prod daemon).',
  '- Every line number in the audit is STALE (computed 59 commits ago). Locate code by SYMBOL NAME or STRING LITERAL, never by the cited line. Report the REAL current line you actually found.',
  '- Per claim assign BOTH:',
  '    verdict in {CONFIRMED, REFUTED, PARTIAL, UNVERIFIABLE}',
  '    drift_attribution in {STILL_TRUE, FIXED_SINCE, NUMBERS_DRIFTED, NEVER_TRUE, NOT_APPLICABLE}',
  '      STILL_TRUE = holds at HEAD as audited. FIXED_SINCE = was true at ' + BASE + ' but a later commit changed/fixed it. NUMBERS_DRIFTED = substance holds but specific numbers/anchors are off. NEVER_TRUE = wrong even at audit time. NOT_APPLICABLE = no drift dimension.',
  '- DRIFT METHOD: before you REFUTE anything, run: git log --oneline ' + BASE + '..HEAD -- <file>   on the relevant file(s). If the file changed since the audit, prefer FIXED_SINCE (name the commit) over NEVER_TRUE.',
  '- Evidence MUST cite the REAL current file:line and QUOTE the decisive code/text. No verdict without quoted evidence.',
  '- A "CLOSED"/safety claim demands a careful confirming read; a false "still safe / still closed" is the costly error here.',
  '',
  'Output exactly ONE verdict object per claim ID listed below — do not merge or drop any.',
  '',
  'CLAIMS TO VERIFY:',
  '',
].join('\n')

const VERDICT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          id: { type: 'string' },
          claim: { type: 'string' },
          verdict: { type: 'string', enum: ['CONFIRMED', 'REFUTED', 'PARTIAL', 'UNVERIFIABLE'] },
          drift_attribution: { type: 'string', enum: ['STILL_TRUE', 'FIXED_SINCE', 'NUMBERS_DRIFTED', 'NEVER_TRUE', 'NOT_APPLICABLE'] },
          evidence: { type: 'string' },
          notes: { type: 'string' },
        },
        required: ['id', 'claim', 'verdict', 'drift_attribution', 'evidence', 'notes'],
      },
    },
  },
  required: ['verdicts'],
}

const GROUPS = [
  {
    key: 'validation',
    effort: 'low',
    ids: ['V1', 'V2', 'V3', 'V4', 'M4'],
    prompt: [
      'Run ONLY these commands. Do NOT run the full suite.',
      'V1: run  uvx ruff check claw_v2 tests  -> audit says ALL CHECKS PASSED. Report result.',
      'V2: run  uvx ruff format --check claw_v2 tests  -> audit says 15 files would reformat (6 runtime + 9 tests; runtime: browser_tools, coordinator, diagnostics, f2_recovery, morning_brief, task_ledger). Report exact count and which runtime files.',
      'V3: run  .venv/bin/python -m pytest tests/test_architecture_invariants.py -p no:cacheprovider -q  -> audit says 39 passed / 22 subtests. Report counts.',
      'V4: full-suite result (audit: 3 failed, 3625 passed, 1 skipped). DO NOT RUN THE FULL SUITE (forbidden, daemon restart risk). The 3 specific test_bot.py failures were independently confirmed failing at HEAD by the operator; but the 3625-passed TOTAL is point-in-time at ' + BASE + ' and cannot be re-confirmed without the forbidden run. Mark PARTIAL / NUMBERS_DRIFTED and explain.',
      'M4: run  wc -l claw_v2/bot.py claw_v2/bot_helpers.py claw_v2/task_handler.py  -> audit: 11399 / 3771 / 3164. Report current counts. The substance (bot.py >11k god-module) is what matters; exact numbers drift.',
    ].join('\n'),
  },
  {
    key: 'diagnostics-acks',
    effort: 'high',
    ids: ['D1', 'L_acks_purge'],
    prompt: [
      'D1 (April blocker, audit says CLOSED): acks TOCTOU fixed in claw_v2/diagnostics.py. Confirm by reading the real code: (a) ack-file lock via fcntl.flock on a sidecar (symbol like _ack_file_lock); (b) atomic write mkstemp->fsync->os.replace (symbol like _atomic_write_json); (c) PRAGMA busy_timeout=5000 on the read-only connection; (d) purge-on-write of expired acks. Quote each. Safety-critical: confirm carefully.',
      'L_acks_purge (audit Low): diagnostics acks are purge-on-WRITE-only; expired acks persist on disk if no new acks are written. Read the purge logic and confirm whether purge runs only inside the write path (no standalone/time-based sweep).',
    ].join('\n'),
  },
  {
    key: 'notebook-coordinator',
    effort: 'high',
    ids: ['D3'],
    prompt: [
      'D3 (April blocker, audit says CLOSED with a NUANCE): notebook coordinator bypass closed in claw_v2/nlm_handler.py. Confirm: (a) a _record_task-like fn creates a row in task_ledger; (b) a verify_profile_evidence-like fn finalizes with a verification_status. THEN READ verify_profile_evidence and judge the audit nuance: is the "verification" merely KEY-PRESENCE (checks fields exist) rather than a real judge/LLM lane? A well-formed but factually wrong notebook review would close as passed. Quote the decisive lines. Also confirm a test_notebook_task_coordinator_contract exists and summarize what it asserts.',
    ].join('\n'),
  },
  {
    key: 'liveness',
    effort: 'high',
    ids: ['D5', 'L_D6'],
    prompt: [
      'D5 (April blocker, audit says CLOSED): daemon liveness. Confirm by reading code: (a) a "/health" route in claw_v2/web_transport.py (grep the string "/health"); (b) an atomic liveness sink in claw_v2/liveness.py; (c) a heartbeat write in claw_v2/lifecycle.py; (d) claw_v2/diagnostics.py consumes the sink and flips to critical on web_thread_dead / heartbeat_stale; (e) a tripwire test_liveness_signal_has_a_consumer. Quote each. Safety-critical: confirm carefully.',
      'L_D6 (audit Low): web_transport stop()/shutdown does join(timeout=5) + a warning but NO terminate/kill escalation; the thread is daemon=True so it does not block process exit. Read the stop/shutdown method in claw_v2/web_transport.py; confirm.',
    ].join('\n'),
  },
  {
    key: 'alerts-dedupe',
    effort: 'medium',
    ids: ['D2'],
    prompt: [
      'D2 (April blocker, audit says still OPEN-Low; appears twice in the audit as D.2 and as a Low): operational_alerts dedupe race. A _last_sent dict is accessed WITHOUT a lock, so a concurrent burst can send 2 notifications instead of 1 (bounded, no data loss). Read claw_v2/operational_alerts.py: find _last_sent and check whether all access is guarded by a threading.Lock. Confirm whether the race is still present at HEAD.',
    ].join('\n'),
  },
  {
    key: 'compaction',
    effort: 'medium',
    ids: ['L5'],
    prompt: [
      'L5 (April item, audit says CLOSED): context compaction. Confirm compact=True is the DEFAULT (a USE_COMPACTION flag) and that compaction is AUTO-TRIGGERED inside store_message. Grep for USE_COMPACTION, compact, and store_message across claw_v2; quote the default and the auto-trigger. Name the module(s).',
    ].join('\n'),
  },
  {
    key: 'coordinator-retry',
    effort: 'medium',
    ids: ['L6', 'L_backoff', 'L_deadcode'],
    prompt: [
      'L6 (audit CLOSED-Low-nit): coordinator worker retry is BOUNDED to 2 attempts + a circuit breaker (not infinite spin). Read the retry loop in claw_v2/coordinator.py (symbol like _execute_worker or a retry cap). Confirm the bound.',
      'L_backoff (audit Low): that same retry has NO inter-retry backoff (it continues without a time.sleep between attempts). Confirm no sleep/backoff between retries (bounded by the cap, low impact).',
      'L_deadcode (audit Low): RetryStuckPolicy is dead code (defined but not instantiated by any runtime module). Grep claw_v2 for RetryStuckPolicy; report all references and whether any runtime (non-test) module instantiates/uses it.',
    ].join('\n'),
  },
  {
    key: 'brief-cron-overlap',
    effort: 'medium',
    ids: ['D4'],
    prompt: [
      'D4 (April blocker, audit left UN-RE-VERIFIED -> actually close it now): brief cron overlap/re-entrancy. The daily-brief cron job could overlap if a prior run is still going. Read claw_v2/morning_brief.py (NOTE: this file has UNCOMMITTED working-tree edits — read the CURRENT file) and how the brief is scheduled in the cron scheduler. Determine whether any overlap/re-entrancy guard exists (a lock, in-progress flag, single-flight, or durable-job enqueue that de-dupes). Assign a verdict on whether the overlap risk exists at HEAD, and note whether the working-tree edit affects this.',
    ].join('\n'),
  },
  {
    key: 'retention-reconcile',
    effort: 'medium',
    ids: ['M1', 'L_reconcile'],
    prompt: [
      'M1 (audit Medium): agent_jobs and agent_tasks have NO retention policy — ZERO "DELETE FROM agent_jobs" / "DELETE FROM agent_tasks" anywhere in claw_v2; terminal rows are never deleted, so the 24/7 daemon grows unbounded (contrast observe_stream which prunes + caps + VACUUMs). Grep claw_v2 for: DELETE FROM agent_jobs, DELETE FROM agent_tasks, and any prune of those tables. Confirm whether a retention/prune exists at HEAD.',
      'L_reconcile (audit Low): _reconcile_orphaned_jobs runs every tick with an N+1 query pattern and no rate-limit (up to ~101 PK lookups/tick; cheap/indexed but unbounded per tick). Find _reconcile_orphaned_jobs; confirm it runs each tick without a rate-limit, contrasting _reconcile_stale_tasks (which the audit says IS rate-limited).',
    ].join('\n'),
  },
  {
    key: 'internal-wiring',
    effort: 'high',
    ids: ['M2', 'M3', 'L_xfail', 'L_describes_commit'],
    prompt: [
      'M2 (audit Medium): INTERNAL_WIRING.md section 5.1 (dispatch order) is STALE. Claim: live _handle_text_body in claw_v2/bot.py was refactored to a ROUTE-TABLE (a dispatch_routes structure) in commits d64e494/d04df7e, but 5.1 still lists the old per-handler chain of 15 handlers. ALSO pre-brain handlers #11 (capability_route) and #16 (nlm) are default-OFF via CLAW_ENABLE_SEMANTIC_PREBRAIN_ROUTES=0, yet 5.1 only documents #9 as default-off. Read BOTH the actual _handle_text_body dispatch in bot.py (does it use a route-table / dispatch_routes?) AND section 5.1 of INTERNAL_WIRING.md. Quote both; judge whether 5.1 matches live code.',
      'M3 (audit Medium): in INTERNAL_WIRING.md section 1, four invariants lack an enforced_by field: triple_and_gating, audit_trail, no_silent_degrade, kairos_external_mutation_gated. Read section 1; report which of these four have vs lack an enforced_by entry.',
      'L_xfail (audit Low): section 5.1 references an xfail that was REMOVED (commit d9706f6 removed the xfail in tests/test_dispatch_routing.py; 0 xfails now in the suite). Check: does 5.1 still mention an xfail? Grep tests/test_dispatch_routing.py for xfail. Confirm.',
      'L_describes_commit (audit Low): the describes_commit / last_verified anchors in INTERNAL_WIRING.md are stale (audit said ~5 behind HEAD). Read the describes_commit value(s); compare to current HEAD ' + HEAD + ' (note: also ~59 behind the audited base). Confirm staleness.',
    ].join('\n'),
  },
  {
    key: 'tripwire-symbols',
    effort: 'medium',
    ids: ['L_tripwire5'],
    prompt: [
      'L_tripwire5 (audit Low): the tripwire tests/test_architecture_invariants.py does NOT sentinel-check 5 symbols named as open TODOs in INTERNAL_WIRING section 7: NON_TOOL_LANES, CRITICAL_TASK_KINDS, DAEMON_AUTO_APPROVE, SECRET_PATH_PATTERNS, _DAEMON_REASON. Grep tests/test_architecture_invariants.py for each of these 5 names; report which (if any) the tripwire references. Confirm the coverage gap.',
    ].join('\n'),
  },
  {
    key: 'docs-staleness',
    effort: 'medium',
    ids: ['M5', 'M6', 'L_runbook'],
    prompt: [
      'M5 (audit Medium): docs/AUDIT_CLOSURE.md is stale/misleading — dated 2026-04-26, status "OPEN — 3 blockers (D.1/D.3/D.5)" even though those are closed in code; line anchors broken (cites bot.py 440-450 but bot.py is >11k lines). Read docs/AUDIT_CLOSURE.md; confirm the date, the OPEN status, the named blockers, and the broken anchors.',
      'M6 (audit Medium): BMad/PRD gap. The PRD (find claw-v2.1-prd.md) v2.1.6 targets bot.py ~200 lines vs ~11k real; docs/architecture/ contains only f2_durability_design.md (no system-architecture doc); ADR log ~1 entry. Locate claw-v2.1-prd.md and list docs/architecture/. Confirm the PRD bot.py target vs reality and the thin docs/architecture + ADR log.',
      'L_runbook (audit Low): OPERATIONS_RUNBOOK says "F2 remains design-only" — contradicting INTERNAL_WIRING (F2.0/F2.1 merged, flag-disabled); plus a smoke example uses --expected-code-version c42ae47 while a status example uses e4a3ee2, both vs the moving HEAD. Find the OPERATIONS_RUNBOOK file; confirm the "design-only" line and the stale code-version examples.',
    ].join('\n'),
  },
  {
    key: 'subprocess-redaction',
    effort: 'medium',
    ids: ['L_subprocess', 'L_codex_redact'],
    prompt: [
      'L_subprocess (audit Low): ~5 callsites in kairos/codex/computer use subprocess.run DIRECTLY (with a timeout) bypassing run_subprocess_bounded — so no process-group kill and no arg redaction. Most important: a keychain read in claw_v2/kairos.py (audit cited line 1326) captures an API key in stdout. Grep claw_v2 for subprocess.run callsites; locate the keychain read (e.g. "security find-generic-password" or keychain) in kairos.py by string; confirm it uses raw subprocess.run not the bounded runner. Count the bypass callsites.',
      'L_codex_redact (audit Low): the Codex adapter surfaces stderr/stdout in errors WITHOUT redaction — a _format_cli_detail-like fn that does not pass output through redact_sensitive. Find it (likely claw_v2 codex adapter module); confirm whether the error-detail path calls redact_sensitive. Latent today (Codex does not print tokens).',
    ].join('\n'),
  },
  {
    key: 'tool-contracts-claudemd',
    effort: 'low',
    ids: ['I_toolcontract', 'I_claudemd'],
    prompt: [
      'I_toolcontract (audit Info): BrowserClick/BrowserType/BrowserScreenshot have NO success_condition -> a ToolContractWarning that "Will become a hard error in F4". (Operator already observed these exact warnings emitted at runtime.) Confirm in claw_v2/tools.py: these 3 tools are registered without a success_condition and the warning text exists. Locate by tool name.',
      'I_claudemd (audit Info): the CLAUDE.md working-tree diff is additive and safe (reinforces invariants + persona-vs-dev separation). Run  git diff -- CLAUDE.md  and judge whether the uncommitted change is purely additive/safe (no removal of safety rules).',
    ].join('\n'),
  },
  {
    key: 'h1-root-cause',
    effort: 'high',
    ids: ['H1'],
    prompt: [
      'H1 (audit High): 3 tests in tests/test_bot.py (class BotTests) fail on main: test_autonomous_coding_message_uses_coordinator, test_task_resume_command_restarts_lost_autonomous_task, test_task_resume_reopens_false_success_autonomous_task. The operator ALREADY CONFIRMED all 3 fail at current HEAD (so verdict=CONFIRMED, drift=STILL_TRUE): they fail in ~2.6s (NOT a timeout, contrary to the audit speculation), with "coordinator.run expected called once, called 0 times".',
      'YOUR JOB is the ROOT CAUSE, statically. The audit attributes the breakage to commit a89096e "Consume F2 recovery plans fail-closed". Run:  git show --stat a89096e  and  git log --oneline ' + BASE + '..HEAD -- claw_v2/task_handler.py claw_v2/bot.py  . Read the resume->coordinator path: when task_handler resumes a LOST or FALSE-SUCCESS task, does the fail-closed recovery-plan consumption now BLOCK the coordinator.run rerun? Determine and quote the decisive code: is this (a) an INTENTIONAL F2 behavior change where the 3 tests are STALE and must be updated to the new contract, or (b) a real REGRESSION where lost tasks no longer restart the coordinator? Put your intentional-vs-regression conclusion + the blocking code in evidence/notes. Keep verdict=CONFIRMED (tests do fail), drift=STILL_TRUE.',
    ].join('\n'),
  },
]

phase('Verify')
log('Verifying ' + GROUPS.reduce((n, g) => n + g.ids.length, 0) + ' audit claims across ' + GROUPS.length + ' read-only Explore agents vs HEAD ' + HEAD)

const results = await parallel(GROUPS.map(g => () =>
  agent(PREAMBLE + g.prompt, {
    label: 'verify:' + g.key,
    phase: 'Verify',
    agentType: 'Explore',
    schema: VERDICT_SCHEMA,
    effort: g.effort,
  }).then(r => ({ group: g.key, expected: g.ids, verdicts: (r && r.verdicts) || [] }))
))

const byGroup = results.filter(Boolean)
const got = []
for (const r of byGroup) for (const v of r.verdicts) got.push(v)
const gotIds = new Set(got.map(v => v.id))
const EXPECTED = GROUPS.flatMap(g => g.ids)
const missing = EXPECTED.filter(id => !gotIds.has(id))
log('verdicts collected: ' + got.length + ' / expected ' + EXPECTED.length + (missing.length ? '  MISSING: ' + missing.join(',') : '  (full coverage)'))

return { verdicts: got, missing, expectedCount: EXPECTED.length, gotCount: got.length, byGroup }
