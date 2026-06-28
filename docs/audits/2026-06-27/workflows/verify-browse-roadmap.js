export const meta = {
  name: 'verify-browse-roadmap-2026-06-27',
  description: 'Read-only verification of the browse-failure + F0-F6 roadmap investigation against current HEAD, with cross-links to the prior audit verification',
  phases: [
    { title: 'Verify', detail: '8 read-only Explore agents verify the browse/roadmap claims and cross-link them to the audit findings' },
  ],
}

const BASE = '6eb6ab9'
const HEAD = '610bfea'

const PREAMBLE = [
  'You verify findings from a recent INVESTIGATION (browse failure + F0-F6 roadmap) against the CURRENT working tree. STRICTLY READ-ONLY.',
  '',
  'CONTEXT:',
  '- Current HEAD is ' + HEAD + '. The live prod daemon is RUNNING (pid 24884). Do not disturb it: only passive reads.',
  '- Some cited line numbers may be stale; locate code by SYMBOL or STRING LITERAL, report the REAL current line.',
  '',
  'HARD RULES:',
  '- READ-ONLY: Read/Grep/Glob, read-only git, and ONLY the specific shell commands your task explicitly lists (e.g. curl to a localhost debug port, gh pr view, wc, grep). NEVER Edit/Write, never mutate files/git/db, never run the full test suite, never restart/kill anything.',
  '- LABEL EVIDENCE CLASS for every claim. Distinguish:',
  '    CODE-VERIFIED = you read or ran it and saw the result;',
  '    OPERATIONAL/FIELD-ASSERTED = deployment or live-env state you CANNOT establish read-only (e.g. "deployed", "flag OFF in the running daemon", "field-proven");',
  '    RUNTIME-ASSERTED = a specific past run / message-ID / canary event you cannot reproduce statically.',
  '  Never assert OPERATIONAL or RUNTIME state as CODE-VERIFIED. Put the class in notes.',
  '- Per claim assign verdict in {CONFIRMED, REFUTED, PARTIAL, UNVERIFIABLE} and drift_attribution in {STILL_TRUE, FIXED_SINCE, NUMBERS_DRIFTED, NEVER_TRUE, NOT_APPLICABLE}.',
  '- Evidence MUST cite the REAL current file:line and QUOTE the decisive code/text. No verdict without quoted evidence.',
  '- cross_link: when your task says CROSS-LINK, state in that field how the claim relates to the prior audit (e.g. "same F2 subsystem as audit H1"); otherwise put "none".',
  '',
  'Output exactly ONE verdict object per claim ID listed below.',
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
          evidence_class: { type: 'string', enum: ['CODE-VERIFIED', 'OPERATIONAL', 'RUNTIME-ASSERTED', 'MIXED'] },
          evidence: { type: 'string' },
          cross_link: { type: 'string' },
          notes: { type: 'string' },
        },
        required: ['id', 'claim', 'verdict', 'drift_attribution', 'evidence_class', 'evidence', 'cross_link', 'notes'],
      },
    },
  },
  required: ['verdicts'],
}

const GROUPS = [
  {
    key: 'browser-rootcause',
    effort: 'high',
    ids: ['B2', 'B3'],
    prompt: [
      'B2 (THE root cause): the coordinator runs research->synthesis->verification ENTIRELY in the advisory "research" lane (read-only, NO tools by design), so browser tools are never invoked. Read claw_v2/coordinator.py around the research/synthesis/verification phases (investigation cited lines 857 and 1060 — locate by symbol) and confirm each phase calls router.ask(lane="research") or an advisory lane. Then grep claw_v2/coordinator.py AND claw_v2/agent_loop.py for: browser_use_task, Browser, chrome_cdp, BrowserClick, BrowserType, BrowserScreenshot, CDP — the claim is ZERO matches (no browser execution step in the coordinator pipeline). Also ground the lane invariant in claw_v2/llm.py: research/verifier/judge are non-tool lanes (e.g. NON_TOOL_LANES / _validate_lane_input) while worker/worker_heavy are tool-capable. Quote code. CROSS-LINK: the prior audit listed the lane-no-tools invariant as "audit-asserted, not independently re-verified" — note whether this behaviorally grounds it.',
      'B3: consequently, coordinator workers receive an "Evidence Pack" carrying only METADATA (artifact_id, job_id, trace_id) with NO feed/page content. Find where the evidence pack / worker input context is assembled in coordinator.py; confirm it carries only metadata identifiers, not fetched browser/page content. Quote the construction.',
    ].join('\n'),
  },
  {
    key: 'chrome-health',
    effort: 'medium',
    ids: ['B1', 'F5_caveat'],
    prompt: [
      'B1: Chrome/CDP is healthy. Run (read-only):  curl -s http://localhost:9250/json/version  and  curl -s http://localhost:9250/json  . Confirm CDP reachable on port 9250 and report the Browser/version string (investigation said Chrome/149). Confirm the dedicated profile exists:  ls -la ~/.claw/chrome-profile  . Confirm the atomic browser tools exist in claw_v2/browser_tools.py (navigate, snapshot, click, type). Mark "last real use navigate+snapshot example.com success" as RUNTIME-ASSERTED.',
      'F5_caveat: the dedicated Chrome had only a google.com tab, NO authenticated X session (so even with browser wired, X auth/anti-bot is a second gate). From the  curl http://localhost:9250/json  output, list the currently open tab URLs and report whether any X.com/twitter session is present or only google/blank. Label CURRENT-RUNTIME-OBSERVED (a snapshot now, not the canary moment).',
    ].join('\n'),
  },
  {
    key: 'f2-and-h1-link',
    effort: 'high',
    ids: ['F2'],
    prompt: [
      'F2 status (roadmap: BUILT, NOT DEPLOYED — tables exist empty, flag OFF, real primary write-path unbuilt). Verify in CODE: (a) the F2 durability flag default — find f2_durability_enabled in claw_v2/config.py (expect default False); (b) the F2 tables/schema are defined — grep for the 4 F2 durability tables; (c) note whether the "primary write-path" is gated/unbuilt. Label live "flag OFF in the running daemon" as OPERATIONAL (you can read the default, not the live env). ',
      'CROSS-LINK TO PRIOR AUDIT (critical): the audit High finding H1 = 3 red tests in tests/test_bot.py whose ROOT CAUSE is the F2 recovery fail-closed logic added in commit a89096e — claw_v2/task_handler.py, the _run_coordinated_task resume branch that reads coordinator.f2_durability_store (~lines 868-927). Read that branch; confirm it is the SAME F2 subsystem the roadmap classifies as "built not deployed" — i.e. the audit red tests and the roadmap F2 row describe the same code. State the linkage explicitly in cross_link.',
    ].join('\n'),
  },
  {
    key: 'f4-and-honesty',
    effort: 'medium',
    ids: ['F4', 'HF2'],
    prompt: [
      'F4 status (roadmap: F4-A + F4-B1 deployed/field-proven; F4-B2 UNBUILT). Verify in CODE: (a) F4-A verify/tooluse timeout (memory: 30s fail-closed) — find it; (b) F4-B1 deterministic delegation — find the CLAW_F4_DETERMINISTIC_DELEGATION flag and the f4b delegation durable job/runner; (c) confirm NO F4-B2 "forced-action general / anti-confabulation post-model" implementation exists (grep). Label "deployed/field-proven" as OPERATIONAL/MEMORY-ASSERTED. CROSS-LINK: the prior audit confirmed F4-B1 is MERGED at HEAD ' + HEAD + ' (PRs #150/#151). Note consistency.',
      'HF2: the honesty chain worked (F4-B1 delegated without confabulating; the verifier BLOCKED the unverified result -> zero invented feed) — this is RUNTIME behavior. Statically confirm the MECHANISM exists: the F4-B1 delegation path AND a verifier path that blocks on missing/insufficient evidence (success-contract / verification gate). CROSS-LINK to audit D3: the audit found notebook verification is KEY-PRESENCE only (weak on content truth). Note the nuance: the machinery blocks MISSING evidence well (HF2) but does not validate evidence TRUTH/quality (D3) — same subsystem, two angles.',
    ].join('\n'),
  },
  {
    key: 'f5-fix-path',
    effort: 'medium',
    ids: ['F5', 'F5_1', 'F6'],
    prompt: [
      'F5 (roadmap: NOT BUILT — browser resilience / structured-output; the browser/computer-use fix lives here). Confirm in CODE there is NO tool-capable browser execution step wired into the coordinator pipeline (cross-ref B2: grep claw_v2/coordinator.py for any worker/worker_heavy lane step that drives browser tools / CDP — expect none).',
      'F5_1 (the fix path): the only artifact is a SPEC DRAFT. Confirm the file exists:  docs/superpowers/specs/2026-06-14-hermes-browser-tooling-adoption.md  . Summarize in one line what it proposes; confirm it is a draft/spec, NOT wired code.',
      'F6 (roadmap: NOT BUILT — Send/Command fan-out, optional metrics-gated). Grep claw_v2 for any Send/Command fan-out implementation; confirm absent / not built.',
    ].join('\n'),
  },
  {
    key: 'f0-f1-f3',
    effort: 'medium',
    ids: ['F0', 'F1', 'F3'],
    prompt: [
      'F0 (roadmap: DEPLOYED — quick wins: atomic scratch writes, observe_stream GC). Verify in CODE: observe_stream prune/GC exists (claw_v2/observe.py prune() + maintenance_vacuum) and atomic scratch writes exist. Label "deployed/field-verified" as OPERATIONAL. (Note: the prior audit already cited observe.py prune() vs the lack of agent_jobs prune — consistent.)',
      'F1 (roadmap: DEPLOYED — single-writer RuntimeDb + watchdog stale-filter). Verify in CODE: a single-writer RuntimeDb abstraction exists; a watchdog stale-filter exists (grep watchdog / stale-filter). Label deployment as OPERATIONAL/MEMORY-ASSERTED.',
      'F3 (roadmap: PARTIAL — resume + stale-reclaim exist; formal per-task lease/heartbeat-lease does NOT). Verify in CODE: task resume path exists (task_handler resume) and stale-reclaim exists (_reconcile_stale_tasks); confirm there is NO formal per-task LEASE mechanism (grep lease). Confirm the PARTIAL classification.',
    ].join('\n'),
  },
  {
    key: 'hf1-pr112',
    effort: 'medium',
    ids: ['HF1'],
    prompt: [
      'HF1 (the user explicitly flagged this for INDEPENDENT verification — do NOT trust the roadmap subagent): a roadmap subagent claimed PR #112 "2 críticos" are SUPERSEDED, but MEMORY still lists them OPEN. Establish ground truth: run  gh pr view 112 --json number,state,title,mergedAt,closedAt  (read-only) and report PR #112 actual state (OPEN/MERGED/CLOSED) + merge status. Also grep the memory dir  /Users/hector/.claude/projects/-Users-hector-Projects-Dr--strange/memory  for "112" and "browser_atomic" and report what the memory says about the 2 críticos. Verdict: is "superseded" SUBSTANTIATED or NOT? (memory/audit listing them open contradicts "superseded").',
    ].join('\n'),
  },
  {
    key: 'b4-failure-modes',
    effort: 'low',
    ids: ['B4'],
    prompt: [
      'B4: two DISTINCT failure modes in the canary (do not conflate). Mode 1 (msg 14406): no browser content -> worker honest refusal -> verification FAILED. Mode 2 (msg 14404): additionally a verifier-lane infra hiccup — Claude SDK 60s timeout + OpenAI fallback with circuit-OPEN due to rate-limit -> verification PENDING (LLM-lane reliability, SEPARATE from the browser gap). The specific msg IDs / canary run are RUNTIME-ASSERTED (not statically reproducible — label clearly). Statically confirm the MECHANISMS are plausible: (a) the verifier lane has a ~60s timeout and an anthropic->openai fallback (claw_v2/llm.py); (b) a circuit breaker opens on rate-limit (ProviderCircuitBreaker). Quote the timeout + fallback + circuit code. Verdict on mechanism-plausibility, with the msg-ID specifics labeled RUNTIME-ASSERTED.',
    ].join('\n'),
  },
]

phase('Verify')
log('Verifying ' + GROUPS.reduce((n, g) => n + g.ids.length, 0) + ' browse/roadmap claims across ' + GROUPS.length + ' read-only agents vs HEAD ' + HEAD)

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
