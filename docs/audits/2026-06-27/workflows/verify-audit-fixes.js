export const meta = {
  name: 'verify-audit-fixes-2026-06-27',
  description: 'Acceptance gate: verify whether each audit/investigation finding was actually remediated by the 8 commits landed since the read-only verification (610bfea -> 10219e7)',
  phases: [
    { title: 'Verify-Fixes', detail: '9 read-only Explore agents check each finding for a correct, landed remediation' },
  ],
}

const BASELINE = '610bfea'   // where the findings were confirmed present
const HEAD = '10219e7'       // current HEAD; fixes (if any) live in BASELINE..HEAD

const PREAMBLE = [
  'You verify whether AUDIT FINDINGS were actually FIXED. STRICTLY READ-ONLY.',
  '',
  'CONTEXT:',
  '- Each finding below was CONFIRMED PRESENT at commit ' + BASELINE + ' by a prior read-only verification.',
  '- Since then, 8 commits landed; HEAD is now ' + HEAD + '. Several commit messages claim fixes. The live prod daemon is running — passive reads only.',
  '- A commit MESSAGE is NOT proof. Verify the code/tests actually RESOLVE the finding. Be adversarial: "harden X" may not make the failing tests green; "add checks" may be a test without the underlying fix.',
  '',
  'HARD RULES:',
  '- READ-ONLY: Read/Grep/Glob, read-only git (log/show/diff), and ONLY the specific shell commands your task explicitly lists (ruff --check, the 3 named pytest node IDs, gh). NEVER Edit/Write, never mutate files/git/db, NEVER run the full test suite, never restart/kill anything.',
  '- To find the fix commit, run: git log --oneline ' + BASELINE + '..HEAD -- <file>   and inspect with git show <sha>. Put the responsible commit (or "none") in fix_commit.',
  '- Locate code by SYMBOL/STRING, not by any cited line number.',
  '- Assign status:',
  '    FIXED = the remediation landed AND the predicate holds (tests pass / code resolves it);',
  '    PARTIAL = partially addressed (e.g. behind a default-OFF flag, unwired, or only one of N callsites);',
  '    NOT_FIXED = finding still present unchanged;',
  '    REGRESSED = a change made it worse / broke something;',
  '    NOT_APPLICABLE = no fix expected / out of scope.',
  '- evidence_class: CODE-VERIFIED / OPERATIONAL / RUNTIME-ASSERTED / MIXED. Never assert deployment/runtime state as code-verified.',
  '- Evidence MUST cite real file:line and QUOTE the decisive code/test result. No status without quoted evidence.',
  '',
  'Output exactly ONE verdict object per finding ID below.',
  '',
  'FINDINGS TO CHECK FOR FIXES:',
  '',
].join('\n')

const SCHEMA = {
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
          finding: { type: 'string' },
          remediation_expected: { type: 'string' },
          status: { type: 'string', enum: ['FIXED', 'PARTIAL', 'NOT_FIXED', 'REGRESSED', 'NOT_APPLICABLE'] },
          fix_commit: { type: 'string' },
          evidence_class: { type: 'string', enum: ['CODE-VERIFIED', 'OPERATIONAL', 'RUNTIME-ASSERTED', 'MIXED'] },
          evidence: { type: 'string' },
          notes: { type: 'string' },
        },
        required: ['id', 'finding', 'remediation_expected', 'status', 'fix_commit', 'evidence_class', 'evidence', 'notes'],
      },
    },
  },
  required: ['verdicts'],
}

const GROUPS = [
  {
    key: 'h1-tests-f2',
    effort: 'high',
    ids: ['H1'],
    prompt: [
      'H1: the 3 tests in tests/test_bot.py (class BotTests) — test_autonomous_coding_message_uses_coordinator, test_task_resume_command_restarts_lost_autonomous_task, test_task_resume_reopens_false_success_autonomous_task — were RED at ' + BASELINE + ' (coordinator.run called 0 times; a MagicMock coordinator auto-created a truthy f2_durability_store, entering the F2 no-run path). Commit 7696c59 "Harden F2 durability recovery checkpoints" MAY or may not address it.',
      'VERIFY EMPIRICALLY — run ONLY these 3 node IDs (temp DB via conftest, safe):  .venv/bin/python -m pytest tests/test_bot.py -p no:cacheprovider -p no:randomly -k "test_autonomous_coding_message_uses_coordinator or test_task_resume_command_restarts_lost_autonomous_task or test_task_resume_reopens_false_success_autonomous_task" -q  . NEVER run the full suite.',
      'status FIXED iff all 3 PASS. If still failing, NOT_FIXED (or REGRESSED if a new failure mode). Then read  git show --stat 7696c59  and the task_handler.py resume branch (getattr coordinator f2_durability_store) to explain WHAT changed and whether it touches the test path. fix_commit = the commit that makes them green, or "none".',
    ].join('\n'),
  },
  {
    key: 'f3-lease',
    effort: 'medium',
    ids: ['F3'],
    prompt: [
      'F3 was PARTIAL at ' + BASELINE + ': resume + stale-reclaim existed but NO formal per-task lease (only time-based mark_stale_running_lost). Commit 9390bc4 "Add formal JobService leases behind feature flag" claims to add it.',
      'VERIFY: git show --stat 9390bc4 ; grep claw_v2 for lease / lease_expiry / lease_until / lease_token / claim. Confirm whether a formal lease mechanism now exists in JobService (jobs.py) / task_ledger, whether it is behind a feature flag and that flag default, and whether it is wired into the reconcile/claim path. status FIXED iff a real lease exists and is reachable; PARTIAL if behind a default-OFF flag or unwired. Quote the lease code + flag default.',
    ].join('\n'),
  },
  {
    key: 'browser-contract',
    effort: 'medium',
    ids: ['I_toolcontract'],
    prompt: [
      'I_toolcontract: at ' + BASELINE + ', BrowserClick/BrowserType/BrowserScreenshot were registered with NO success_condition, emitting a ToolContractWarning ("hard error in F4"). Commit 4ee49c2 "Add Browser tool contract checks" may address it.',
      'VERIFY: git show --stat 4ee49c2 ; read claw_v2/tools.py registrations for the 3 browser tools — do they now carry a success_condition? OR did the commit add a contract-enforcement test/check instead of the actual condition? If a targeted contract test exists, you may run just that test file (read-only). status FIXED iff the 3 tools now have success_condition (warning resolved) OR an enforced check guarantees it; PARTIAL if only a test was added without the conditions; NOT_FIXED if unchanged. Quote.',
    ].join('\n'),
  },
  {
    key: 'f6-fanout',
    effort: 'medium',
    ids: ['F6'],
    prompt: [
      'F6 was "NOT BUILT" at ' + BASELINE + ' (coordinator fixed 4-phase; AgentBus.send is only inter-agent messaging). Commit 8858671 "Implement deterministic F6 fan-out fan-in shadow metadata" adds something.',
      'VERIFY: git show --stat 8858671 ; grep for the F6 fan-out/fan-in implementation. Determine whether it is REAL dynamic fan-out wired into the coordinator, or "shadow metadata" only (observability/metadata recording WITHOUT actual dynamic scheduling), and whether behind a flag. F6 was an OPTIONAL/deferred/metrics-gated item — note that. status FIXED iff real fan-out is wired; PARTIAL if shadow/metadata-only or behind a default-OFF flag. Quote.',
    ].join('\n'),
  },
  {
    key: 'ruff-format',
    effort: 'low',
    ids: ['V2'],
    prompt: [
      'V2: at ' + BASELINE + ', 15 files needed reformatting (6 runtime: browser_tools, coordinator, diagnostics, f2_recovery, morning_brief, task_ledger + 9 tests). Run  uvx ruff format --check claw_v2 tests  . status FIXED iff it reports 0 files to reformat (all formatted). Report the exact result and which commit cleaned them ( git log --oneline ' + BASELINE + '..HEAD -- claw_v2/coordinator.py claw_v2/morning_brief.py ).',
    ].join('\n'),
  },
  {
    key: 'doc-hygiene',
    effort: 'medium',
    ids: ['M5', 'M2', 'L_xfail', 'L_runbook', 'M3', 'L_tripwire5', 'M6'],
    prompt: [
      'Check each documentation/tripwire finding for a landed fix (git log --oneline ' + BASELINE + '..HEAD -- <file> per file):',
      'M5: FIXED iff docs/AUDIT_CLOSURE.md no longer presents OPEN/3-blockers (D.1/D.3/D.5) as CURRENT status (a banner, re-emit, or status flip). (Spot-check shows it STILL says "Status: OPEN — conditional close pending three blockers" → likely NOT_FIXED; confirm and quote.)',
      'M2: FIXED iff INTERNAL_WIRING.md section 5.1 now documents handler #11 (capability_route) default-off AND/OR a test pins the dispatch order. Check section 5.1 + tests/test_dispatch_routing.py.',
      'L_xfail: FIXED iff INTERNAL_WIRING section 5.1 no longer references an xfail.',
      'L_runbook: FIXED iff OPERATIONS_RUNBOOK no longer says "F2 remains design-only".',
      'M3: FIXED iff the 4 section-1 invariants (triple_and_gating, audit_trail, no_silent_degrade, kairos_external_mutation_gated) now have an enforced_by field OR a new tripwire references them.',
      'L_tripwire5: FIXED iff tests/test_architecture_invariants.py now sentinel-checks the 5 symbols NON_TOOL_LANES, CRITICAL_TASK_KINDS, DAEMON_AUTO_APPROVE, SECRET_PATH_PATTERNS, _DAEMON_REASON.',
      'M6: FIXED iff the PRD (claw-v2.1-prd.md) bot.py target was updated OR docs/architecture/ gained a system-architecture doc.',
    ].join('\n'),
  },
  {
    key: 'security-runtime-tail',
    effort: 'medium',
    ids: ['L_subprocess', 'L_codex_redact', 'D2', 'L_reconcile', 'L_backoff', 'L_acks_purge'],
    prompt: [
      'Check each security/runtime finding for a landed fix (git log --oneline ' + BASELINE + '..HEAD -- <file>):',
      'L_subprocess: FIXED iff the kairos.py keychain read (HEYGEN_API_KEY, "security find-generic-password") now goes through run_subprocess_bounded instead of raw subprocess.run. grep + read.',
      'L_codex_redact: FIXED iff claw_v2/adapters/codex.py _format_cli_detail now passes stderr/stdout through redact_sensitive.',
      'D2: FIXED iff claw_v2/operational_alerts.py _last_sent is now guarded by a threading.Lock.',
      'L_reconcile: FIXED iff _reconcile_orphaned_jobs (daemon.py) is now rate-limited (gated like _reconcile_stale_tasks).',
      'L_backoff: FIXED iff an inter-retry sleep/backoff was added to the coordinator worker retry loop (coordinator.py _execute_worker).',
      'L_acks_purge: FIXED iff a standalone/time-based ack purge was added to diagnostics.py (not just purge-on-write).',
    ].join('\n'),
  },
  {
    key: 'verify-brief-policy',
    effort: 'medium',
    ids: ['D3', 'D4', 'HF1'],
    prompt: [
      'D3 (notebook verification key-presence only): FIXED iff notebook/profile verification now does MORE than key-presence — a judge/content-validation lane (verification_profiles.py / the notebooklm verifier). NOT_FIXED if still bool(evidence.get(key)) presence + count equality.',
      'D4 (brief cron re-entrancy): FIXED iff a re-entrancy guard (lock / in-progress flag / single-flight / dedup enqueue) was added to morning_brief run_if_due. NOTE commit b41bd68 "Make morning brief conversational" touched this file — git show --stat b41bd68 and check whether it ALSO added a guard or only changed message wording (likely wording only → NOT_FIXED for re-entrancy). Distinguish carefully.',
      'HF1 residual (operator gate on browser click/type): was PARTIAL at ' + BASELINE + ' — requires_human=true added but operator still in allowed_contexts for BrowserClick/BrowserType. FIXED iff operator was removed from allowed_contexts (tool_policies.json) for those tools, OR the requires_human gate provably neutralizes operator self-approval. Check tool_policies.json + the approval gate.',
    ].join('\n'),
  },
  {
    key: 'retention',
    effort: 'medium',
    ids: ['M1'],
    prompt: [
      'M1 (no agent_jobs/agent_tasks retention): at ' + BASELINE + ' there were ZERO "DELETE FROM agent_jobs" / "DELETE FROM agent_tasks" anywhere. FIXED iff an off-tick prune/retention for TERMINAL rows of those tables now exists (mirroring observe_prune). grep claw_v2 for DELETE FROM agent_jobs / agent_tasks and any new prune/retention sweep. NOTE commit 9390bc4 touched JobService (jobs.py) — check whether it added retention/lease-expiry deletes. Distinguish lease-expiry cleanup from terminal-row retention. status FIXED only if terminal terminal-row retention exists; PARTIAL if only lease cleanup; NOT_FIXED if still zero.',
    ].join('\n'),
  },
]

phase('Verify-Fixes')
log('Checking fixes for ' + GROUPS.reduce((n, g) => n + g.ids.length, 0) + ' findings across ' + GROUPS.length + ' agents; baseline ' + BASELINE + ' -> HEAD ' + HEAD)

const results = await parallel(GROUPS.map(g => () =>
  agent(PREAMBLE + g.prompt, {
    label: 'fix:' + g.key,
    phase: 'Verify-Fixes',
    agentType: 'Explore',
    schema: SCHEMA,
    effort: g.effort,
  }).then(r => ({ group: g.key, expected: g.ids, verdicts: (r && r.verdicts) || [] }))
))

const byGroup = results.filter(Boolean)
const got = []
for (const r of byGroup) for (const v of r.verdicts) got.push(v)
const gotIds = new Set(got.map(v => v.id))
const EXPECTED = GROUPS.flatMap(g => g.ids)
const missing = EXPECTED.filter(id => !gotIds.has(id))
const tally = {}
for (const v of got) tally[v.status] = (tally[v.status] || 0) + 1
log('fix verdicts: ' + got.length + '/' + EXPECTED.length + '  ' + JSON.stringify(tally) + (missing.length ? '  MISSING: ' + missing.join(',') : '  (full coverage)'))

return { verdicts: got, tally, missing, expectedCount: EXPECTED.length, gotCount: got.length, byGroup }
