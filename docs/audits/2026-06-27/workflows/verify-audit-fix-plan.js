export const meta = {
  name: 'verify-audit-fix-plan',
  description: 'Validate the 2026-06-27 long-tail audit-fix plan (5 checkpoints) against the actual working-tree code: static review, focal-gate runs, adversarial + logical-differential verify, claims audit, synthesis.',
  whenToUse: 'Run to validate the uncommitted M1/L_subprocess/L_codex_redact/D2/L_reconcile remediation against the code before commit. Read-only review: never stages/commits; runs only focal gates (no full suite).',
  phases: [
    { title: 'Review', detail: 'static per-checkpoint code review vs the diff + dependencies' },
    { title: 'Verify', detail: 'run focal gates + adversarial/logical-differential + empirical checks' },
    { title: 'Gates', detail: 'cross-cutting gates (architecture invariants, lifecycle) + plan-claims audit' },
    { title: 'Synthesize', detail: 'reconcile all evidence into per-checkpoint + overall verdict' },
  ],
}

const REPO = '/Users/hector/Projects/Dr.-strange'
const PY = REPO + '/.venv/bin/python'

// ---- shared constraints every agent must honor ----
const RULES = `
HARD CONSTRAINTS (this is a READ-ONLY review of an uncommitted plan):
- Do NOT git add / commit / stash / checkout / reset / push or mutate tracked source in the shared checkout. Read-only git only ('git diff', 'git show', 'git status', 'git log').
- Do NOT run the full suite ('pytest tests/' or 'pytest tests/ -q'). The SessionStart hook warned the full suite can restart the LIVE production daemon. Run ONLY the specific focal test files named in your task.
- Repo root: ${REPO}. Python: ${PY}. Run pytest from repo root, e.g.: cd ${REPO} && ${PY} -m pytest <file> -q
- Evidence standard: a claim is VERIFIED only with concrete primary evidence (file:line, a quoted line of code, or actual command output). "The commit message says it was hardened" or "the test passes" is NOT sufficient on its own — a test that passes but does not ASSERT the fixed behavior proves nothing. Be adversarial.
- The 5 checkpoints claim to fix findings that 'docs/audits/2026-06-27/fix-verification.md' verified as NOT_FIXED at HEAD. The production+test changes are in the WORKING TREE (uncommitted). HEAD therefore = pre-fix baseline; you can read pre-fix source with 'git show HEAD:<path>'.
`

const REVIEW_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['checkpoint_id', 'claim_implemented', 'dependency_checks', 'code_findings', 'preliminary_verdict', 'notes'],
  properties: {
    checkpoint_id: { type: 'string' },
    claim_implemented: { type: 'boolean', description: 'Does the diff actually implement what the plan claims for this checkpoint?' },
    dependency_checks: {
      type: 'array',
      description: 'Each load-bearing assumption the code relies on (columns exist, signatures/kwargs match, import removed safely, time imported, etc.)',
      items: {
        type: 'object', additionalProperties: false,
        required: ['check', 'result', 'evidence'],
        properties: {
          check: { type: 'string' },
          result: { type: 'string', enum: ['pass', 'fail', 'unknown'] },
          evidence: { type: 'string', description: 'file:line + quoted code or command output' },
        },
      },
    },
    code_findings: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['severity', 'title', 'location', 'detail'],
        properties: {
          severity: { type: 'string', enum: ['blocker', 'major', 'minor', 'nit'] },
          title: { type: 'string' },
          location: { type: 'string', description: 'file:line' },
          detail: { type: 'string' },
        },
      },
    },
    preliminary_verdict: { type: 'string', enum: ['VERIFIED', 'PARTIAL', 'DEFECT', 'NOT_VERIFIED'] },
    notes: { type: 'string' },
  },
}

const VERIFY_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['checkpoint_id', 'test_runs', 'tests_prove_claim', 'differential', 'empirical_checks', 'adversarial_findings', 'verdict', 'rationale'],
  properties: {
    checkpoint_id: { type: 'string' },
    test_runs: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['command', 'passed', 'failed', 'errors', 'status', 'tail'],
        properties: {
          command: { type: 'string' },
          passed: { type: 'integer' },
          failed: { type: 'integer' },
          errors: { type: 'integer' },
          status: { type: 'string', enum: ['PASS', 'FAIL', 'ERROR'] },
          tail: { type: 'string', description: 'last ~5 lines of pytest output' },
        },
      },
    },
    tests_prove_claim: { type: 'boolean', description: 'Do the NEW tests actually assert the fixed behavior (would they fail without the fix)?' },
    differential: {
      type: 'object', additionalProperties: false,
      required: ['method', 'prefix_lacks_fix', 'test_asserts_fixed_behavior', 'conclusion', 'evidence'],
      properties: {
        method: { type: 'string', enum: ['logical-git-show', 'runtime-prefix-run', 'not-applicable'] },
        prefix_lacks_fix: { type: 'boolean', description: 'Confirmed via git show HEAD:<file> that pre-fix code lacks the fix' },
        test_asserts_fixed_behavior: { type: 'boolean' },
        conclusion: { type: 'string', enum: ['PROVES_FIX', 'DOES_NOT_PROVE', 'INCONCLUSIVE', 'NA'] },
        evidence: { type: 'string' },
      },
    },
    empirical_checks: {
      type: 'array',
      description: 'Direct runtime probes (e.g. call redact_sensitive on a long secret-bearing string and measure length+redaction).',
      items: {
        type: 'object', additionalProperties: false,
        required: ['check', 'result', 'evidence'],
        properties: {
          check: { type: 'string' },
          result: { type: 'string', enum: ['pass', 'fail', 'na'] },
          evidence: { type: 'string' },
        },
      },
    },
    adversarial_findings: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['severity', 'title', 'detail'],
        properties: {
          severity: { type: 'string', enum: ['blocker', 'major', 'minor', 'nit'] },
          title: { type: 'string' },
          detail: { type: 'string' },
        },
      },
    },
    verdict: { type: 'string', enum: ['VERIFIED', 'PARTIAL', 'DEFECT', 'NOT_VERIFIED'] },
    rationale: { type: 'string' },
  },
}

const GATES_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['gates', 'architecture_invariants_status', 'overall', 'findings'],
  properties: {
    gates: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['name', 'command', 'passed', 'failed', 'errors', 'status', 'tail'],
        properties: {
          name: { type: 'string' },
          command: { type: 'string' },
          passed: { type: 'integer' },
          failed: { type: 'integer' },
          errors: { type: 'integer' },
          status: { type: 'string', enum: ['PASS', 'FAIL', 'ERROR'] },
          tail: { type: 'string' },
        },
      },
    },
    architecture_invariants_status: { type: 'string', enum: ['PASS', 'FAIL', 'ERROR'], description: 'Specifically whether the new synchronous durable_retention_prune scheduler handler + the kairos AST contract pass the tripwire suite.' },
    overall: { type: 'string', enum: ['PASS', 'FAIL'] },
    findings: { type: 'array', items: { type: 'string' } },
  },
}

const CLAIMS_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['claims', 'discrepancies', 'overall_honest'],
  properties: {
    claims: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['claim', 'verified', 'evidence'],
        properties: {
          claim: { type: 'string' },
          verified: { type: 'boolean' },
          evidence: { type: 'string' },
        },
      },
    },
    discrepancies: { type: 'array', items: { type: 'string' } },
    overall_honest: { type: 'boolean', description: 'Are the plan\'s status/verification claims an honest representation of the working-tree state?' },
  },
}

const SYNTH_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['per_checkpoint', 'cross_cutting_summary', 'claims_audit_summary', 'overall_verdict', 'blockers', 'recommended_next_steps', 'headline'],
  properties: {
    headline: { type: 'string', description: 'One-paragraph bottom line for the user.' },
    per_checkpoint: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['id', 'title', 'verdict', 'confidence', 'one_line', 'key_evidence', 'blocking_issues'],
        properties: {
          id: { type: 'string' },
          title: { type: 'string' },
          verdict: { type: 'string', enum: ['VERIFIED', 'PARTIAL', 'DEFECT', 'NOT_VERIFIED'] },
          confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
          one_line: { type: 'string' },
          key_evidence: { type: 'string' },
          blocking_issues: { type: 'array', items: { type: 'string' } },
        },
      },
    },
    cross_cutting_summary: { type: 'string' },
    claims_audit_summary: { type: 'string' },
    overall_verdict: { type: 'string', enum: ['SHIP', 'SHIP_WITH_FIXES', 'DO_NOT_SHIP'] },
    blockers: { type: 'array', items: { type: 'string' } },
    recommended_next_steps: { type: 'array', items: { type: 'string' } },
  },
}

// ---- the 5 checkpoints, with explicit per-checkpoint risk checklists ----
const CHECKPOINTS = [
  {
    id: 'C1', title: 'M1 — Durable retention (prune_terminal + scheduler)',
    files: ['claw_v2/jobs.py', 'claw_v2/task_ledger.py', 'claw_v2/main.py'],
    focal: ['tests/test_jobs.py', 'tests/test_task_ledger.py', 'tests/test_latency_audit_group3.py'],
    claim: 'Adds JobService.prune_terminal and TaskLedger.prune_terminal (delete OLD terminal rows, bounded by max_rows, gated by retention_days), an hourly scheduler job "durable_retention_prune", and env config DURABLE_RETENTION_DAYS / DURABLE_RETENTION_PRUNE_MAX_ROWS. Goal: stop unbounded growth of agent_jobs / agent_tasks (now fed by F4-B1).',
    risks: [
      'CRITICAL: the prune SQL orders/filters by COALESCE(completed_at, updated_at, created_at). Confirm ALL THREE columns exist on BOTH agent_jobs (jobs.py schema) AND agent_tasks (task_ledger.py schema). A missing column = OperationalError at runtime.',
      'JobService.prune_terminal uses self._retry_after_disk_io + self.observe.emit; TaskLedger.prune_terminal uses self._lock + self._emit directly. Confirm each matches its module\'s established locking/commit/observe pattern (e.g. does TaskLedger have/justify NOT using a disk-io retry wrapper?).',
      'Boundedness: confirm the DELETE is genuinely capped per call (LIMIT max_rows via subselect) and cannot full-scan/delete-all. Confirm max_rows<=0 disables (returns 0) and retention_days<=0 is clamped.',
      'main.py _durable_retention_prune_handler runs INLINE in CronScheduler.run_due (synchronous, on-tick). Core Invariant 1 forbids heavy/LLM/subprocess work on-tick. A bounded SQLite DELETE is the same shape as the existing observe_prune handler (precedent). Confirm it does NOT do subprocess/LLM/unbounded work, and that test_architecture_invariants still passes (the Gates agent runs that file — flag the concern here).',
      'env parsing _env_int: default = min(JOB,TASK) consts (30 days / 20000 rows); ValueError falls back to default; negatives clamped to 0. Confirm.',
    ],
    test_focus: 'Confirm the NEW tests INSERT terminal rows older than the cutoff and ASSERT they are deleted, that rows newer than the cutoff SURVIVE (retention boundary), and that the per-call max_rows cap is respected (boundedness). A test that only asserts prune()->0 on an empty table proves nothing.',
    differential: false,
  },
  {
    id: 'C2', title: 'L_subprocess — kairos.py raw subprocess.run eliminated',
    files: ['claw_v2/kairos.py', 'tests/test_kairos.py', 'tests/test_kairos_health_check.py'],
    focal: ['tests/test_kairos.py', 'tests/test_kairos_health_check.py', 'tests/test_subprocess_runner.py'],
    claim: 'Replaces 5 raw subprocess.run calls in kairos.py (gh api; git rev-parse; git push; security find-generic-password for HEYGEN_API_KEY; pgrep) with run_subprocess_bounded; removes "import subprocess"; adds an AST contract in test_kairos.py forbidding subprocess.run reintroduction in kairos.py.',
    risks: [
      'Return-shape compatibility: every call site uses result.stdout / result.returncode (and push.returncode, key_result.stdout, proc.stdout). Confirm run_subprocess_bounded (claw_v2/subprocess_runner.py) returns an object exposing .stdout, .stderr, .returncode with the same semantics as subprocess.CompletedProcess.',
      'Kwargs: call sites pass timeout_s=, observe=, and (keychain) max_output_chars=2048. Confirm these are the REAL parameter names of run_subprocess_bounded.',
      'CRITICAL: "import subprocess" was removed. Grep kairos.py for any remaining reference to `subprocess` (subprocess.CalledProcessError, subprocess.PIPE, type hints, the _format... helpers) — a single leftover reference = NameError at runtime.',
      'Keychain call: max_output_chars=2048 must not truncate a real HEYGEN_API_KEY (keys are short). Confirm the arg list to `security find-generic-password` is unchanged.',
      'AST contract test: confirm it actually parses kairos.py into an AST and FAILS when a subprocess.run/Popen Call node is present in kairos.py (not a trivially-true assertion), and that it is scoped to kairos.py.',
    ],
    test_focus: 'Confirm test_kairos / test_kairos_health_check actually EXECUTE the converted code paths (a signature mismatch only surfaces if the path runs, not if it is mocked away). Confirm the AST contract test is meaningful.',
    differential: false,
  },
  {
    id: 'C3', title: 'L_codex_redact — Codex CLI stdout/stderr redaction',
    files: ['claw_v2/adapters/codex.py', 'tests/test_codex_adapter.py'],
    focal: ['tests/test_codex_adapter.py'],
    claim: '_format_cli_detail now redacts stdout/stderr via redact_sensitive; artifacts["stderr"] also redacted; tests assert sk-... style tokens do not leak in stdout/stderr.',
    risks: [
      'BLOCKER CANDIDATE (settle empirically): old code did `result.stderr.strip()[:200]`; new code does `str(redact_sensitive(result.stderr.strip(), limit=200))`. Does redact_sensitive(text, limit=N) actually TRUNCATE to ~N chars, or does `limit` only govern redaction window? If it does NOT truncate, artifacts["stderr"] REGRESSED from bounded ([:200]) to UNBOUNDED — there is no outer cap on that field (unlike _format_cli_detail, which is saved by its trailing detail[:500]). EMPIRICALLY call redact_sensitive("A"*1000 + " sk-LIVESECRETabc123", limit=200) using ' + PY + ' and report (a) len(result) and (b) whether the sk- token is gone. This determines C3\'s verdict.',
      'Why the `str(...)` wrapper? Inspect redact_sensitive\'s return type (claw_v2/redaction.py) — if it can return a non-str, confirm str() is correct and that downstream (artifacts dict / detail string) tolerates it.',
      '_format_cli_detail final return is detail[:500]; confirm the combined "stderr | stdout: stdout" branch still cannot exceed 500.',
    ],
    test_focus: 'Confirm the new tests assert a planted sk-... token is ABSENT from the produced stderr/stdout/detail, AND ideally that the field length is bounded. Token-absence assertion is the differential proof.',
    differential: true,
  },
  {
    id: 'C4', title: 'D2 — operational_alerts _last_sent race fixed',
    files: ['claw_v2/operational_alerts.py', 'tests/test_operational_alerts.py'],
    focal: ['tests/test_operational_alerts.py'],
    claim: 'Adds a threading.Lock around _last_sent; RESERVES the cooldown slot (sets _last_sent[key]=now) under the lock BEFORE calling notify; if notify raises, ROLLS BACK the reservation (pops key iff still == now). Adds a deterministic 2-thread test. Goal: no duplicate alert within the cooldown under concurrency.',
    risks: [
      'Reserve-before-notify correctness: two concurrent calls, same dedupe_key — thread A reserves now; thread B reads within cooldown and suppresses. Confirm there is no longer a check-then-set TOCTOU window (the old code did get() ... then notify() ... then set()).',
      'Rollback edge: on notify failure, pop happens iff _last_sent.get(key) == now. If clock() yields the SAME value for two threads and one fails+rolls back, could it pop the OTHER thread\'s live reservation (clobbering its cooldown)? Assess; severity likely minor but note it.',
      'Confirm notify() is still called OUTSIDE the lock (so a slow/blocking notify does not serialize all alerts), and _emit_status ordering is unchanged.',
    ],
    test_focus: 'Confirm the test is genuinely concurrent (2 threads with a barrier/event forcing interleaving) and asserts notify fires EXACTLY ONCE within the cooldown. Assess whether it is deterministic enough to reliably fail against the pre-fix (lock-less) code; if concurrency makes the differential non-deterministic, mark INCONCLUSIVE rather than DOES_NOT_PROVE.',
    differential: true,
  },
  {
    id: 'C5', title: 'L_reconcile — orphan-job reconciliation rate-limited',
    files: ['claw_v2/daemon.py', 'tests/test_daemon.py'],
    focal: ['tests/test_daemon.py'],
    claim: 'Adds orphan_job_reconciliation_interval (default 5*60); tick() now passes now= into _reconcile_orphaned_jobs; the reconciler skips (returns 0) if called again within the interval. Claim: cancellation semantics and JobService are UNCHANGED.',
    risks: [
      'Confirm `time` is imported in daemon.py (the guard uses time.time() when now is None).',
      'Confirm tick() correctly threads its own `now` into _reconcile_orphaned_jobs(now=now) and that the first call after construction (_last_orphan_job_reconciliation_at = 0.0) runs.',
      'CRITICAL (no silent semantic change): confirm the reconciler BODY below the new interval guard is byte-identical to the pre-fix version — i.e. only an early-return guard was prepended, the cancellation/relisting logic is untouched. Use git show HEAD:claw_v2/daemon.py to compare.',
      'Inspect the test_daemon.py DIFF ITSELF (not just "41 passed"): were any EXISTING assertions weakened/modified to accommodate the every-tick->interval change (e.g. an existing test that asserted orphan reconcile runs every tick)? +17 lines could be all-new tests OR edited assertions. Distinguish.',
    ],
    test_focus: 'Confirm the new test asserts: first tick reconciles; an immediate second tick within the interval does NOT (returns 0 / no rescan); a tick after the interval reconciles again. And confirm no pre-existing assertion was relaxed.',
    differential: false,
  },
]

function staticPrompt(cp) {
  return `${RULES}

You are statically reviewing ONE checkpoint of an uncommitted audit-fix plan, validating the plan's claim against the ACTUAL working-tree code.

CHECKPOINT ${cp.id}: ${cp.title}
PLAN CLAIM: ${cp.claim}
FILES CHANGED: ${cp.files.join(', ')}

Do this:
1. Read the diff: cd ${REPO} && git diff -- ${cp.files.join(' ')}
2. Read enough surrounding code AND dependencies to verify the claim is implemented correctly and safely. Read the pre-fix baseline with 'git show HEAD:<path>' where useful.
3. Resolve EACH of these checkpoint-specific risks with primary evidence (file:line + quoted code or command output):
${cp.risks.map((r, i) => `   ${i + 1}. ${r}`).join('\n')}

Be adversarial and surgical. Report dependency_checks (one per load-bearing assumption, esp. the CRITICAL ones), code_findings (real defects/risks only — not style preferences unrelated to the change), and a preliminary_verdict for whether the CODE correctly implements the claim (test execution happens in a later stage; judge the code here). Return ONLY the structured object.`
}

function verifyPrompt(review, cp) {
  return `${RULES}

You are the VERIFY stage for ONE checkpoint. A static reviewer already inspected the code; your job is to (a) RUN the focal tests, (b) decide adversarially whether the tests actually PROVE the fix, (c) run any empirical probe, and (d) for differential checkpoints, establish whether the new test must fail against pre-fix code.

CHECKPOINT ${cp.id}: ${cp.title}
PLAN CLAIM: ${cp.claim}

STATIC REVIEWER'S OUTPUT (JSON):
${JSON.stringify(review)}

Do this:
1. Run EACH focal test file (from repo root), capture the pytest summary (passed/failed/errors) and the last lines:
${cp.focal.map((f) => `   cd ${REPO} && ${PY} -m pytest ${f} -q`).join('\n')}
   (Run ONLY these files. Never run the whole suite.)
2. tests_prove_claim: ${cp.test_focus}
   Read the NEW test code (git diff -- ${cp.files.filter((f) => f.startsWith('tests/')).join(' ')}) and judge whether the assertions would FAIL if the production fix were absent. If they would pass regardless of the fix, tests_prove_claim=false.
${cp.id === 'C3' ? `3. EMPIRICAL (decisive for C3): run a one-off probe of redact_sensitive's truncation+redaction. Example:
   cd ${REPO} && ${PY} -c "from claw_v2.redaction import redact_sensitive as r; s='A'*1000+' sk-LIVESECRETabc123'; out=str(r(s, limit=200)); print('LEN', len(out)); print('TOKEN_PRESENT', 'sk-LIVESECRETabc123' in out)"
   Report LEN and TOKEN_PRESENT in empirical_checks. If LEN is ~1000 (not truncated to ~200), the artifacts["stderr"] field is UNBOUNDED -> this is at least a 'major' adversarial finding and likely caps the verdict at PARTIAL or DEFECT for that field, EVEN IF the token is redacted. Decide the verdict accordingly.` : '3. empirical_checks: add any direct runtime probe that strengthens/weakens the claim, else mark na.'}
${cp.differential ? `4. DIFFERENTIAL (method=logical-git-show): read the PRE-FIX production source with 'git show HEAD:${cp.files[0]}' and confirm it LACKS the fix (prefix_lacks_fix). Then confirm the new test ASSERTS the fixed behavior (test_asserts_fixed_behavior). If pre-fix emits the unsafe behavior AND the test asserts the safe behavior, conclusion=PROVES_FIX (the test must fail pre-fix by construction). Do NOT attempt to run the test against a worktree/pre-fix source — the editable claw_v2 install makes that import-ambiguous and unreliable; the logical differential via git show + assertion analysis is the sound method here. For C4 specifically: if the concurrency test would only fail pre-fix non-deterministically, set conclusion=INCONCLUSIVE.` : '4. differential: set method=not-applicable, conclusion=NA (this checkpoint is not a security/race differential).'}

Then give a verdict for the checkpoint as a whole:
- VERIFIED = code correct AND tests pass AND tests prove the fix (and, where applicable, empirical/differential confirm).
- PARTIAL = works but with a real caveat (e.g. a bounded-field regression, a test that doesn't fully prove it, a minor race edge).
- DEFECT = a real bug/regression introduced.
- NOT_VERIFIED = could not establish correctness / tests fail.
Return ONLY the structured object.`
}

const gatesThunk = () =>
  agent(
    `${RULES}

You run the CROSS-CUTTING gates for this review. Run EXACTLY these two focal files (never the full suite) and report results:
1. tests/test_architecture_invariants.py  — the tripwire suite. It AST-scans runtime code for invariant violations. This is the gate that would catch (a) the NEW synchronous 'durable_retention_prune' scheduler handler in main.py if it illegally does heavy/subprocess/LLM work on-tick, and (b) whether the kairos subprocess AST contract integrates cleanly. If it FAILS, read the named invariant in claw_v2/INTERNAL_WIRING.md §1 and report WHICH invariant and whether it's a true violation by the plan's changes or a pre-existing/unrelated failure.
2. tests/test_lifecycle.py — boot/wiring sanity (the new scheduler job is registered in main.py _setup_scheduler).

Commands:
   cd ${REPO} && ${PY} -m pytest tests/test_architecture_invariants.py -q
   cd ${REPO} && ${PY} -m pytest tests/test_lifecycle.py -q

Report each gate's passed/failed/errors + tail, set architecture_invariants_status precisely, and overall PASS only if both pass. Return ONLY the structured object.`,
    { phase: 'Gates', schema: GATES_SCHEMA, label: 'gates:cross-cutting' }
  )

const claimsThunk = () =>
  agent(
    `${RULES}

You AUDIT the plan's own process/status claims for honesty against the working-tree state. Verify each with read-only git:

1. "git diff --check: limpio" -> run: cd ${REPO} && git diff --check ; (empty output = clean).
2. "No hice commit ni staging" -> run: cd ${REPO} && git status --short && git diff --cached --stat && git log --oneline -1 . Verify: files are MODIFIED-unstaged (left column space, e.g. " M"), the staging area (git diff --cached) is EMPTY, and HEAD is unchanged (no new commit from this plan; HEAD should be the pre-existing tip).
3. The plan's changed-file list matches reality. The plan claims exactly these changed under claw_v2/: adapters/codex.py, daemon.py, jobs.py, kairos.py, main.py, operational_alerts.py, task_ledger.py — plus their tests: test_codex_adapter.py, test_daemon.py, test_jobs.py, test_kairos.py, test_kairos_health_check.py, test_latency_audit_group3.py, test_operational_alerts.py, test_task_ledger.py. Run git status --short and confirm NO unexpected extra tracked-file modifications and none missing.
4. "Untracked previos quedaron intactos": confirm .patch-backups/, docs/audits/2026-06-27/*, memory/2026-06-*.md remain UNTRACKED (??), i.e. the plan did not accidentally stage/track them.
5. Plan test-count claims (e.g. "test_daemon.py: 41 passed", "257 passed/41 subtests", "267 passed/51 subtests"): you cannot re-run the broad combined gates here (out of scope / time), so mark those as 'not independently re-run' with evidence = the per-file gate runs are handled by other agents. Do NOT assert them true without evidence; verified=false with an explanatory note is the honest answer if you didn't run them.

For each, set verified + evidence (actual command output). List any discrepancies. overall_honest=true only if the plan's status claims match reality. Return ONLY the structured object.`,
    { phase: 'Gates', schema: CLAIMS_SCHEMA, label: 'gates:claims-audit' }
  )

// ---- run: pipeline (review -> verify) per checkpoint, concurrently with the gate agents ----
phase('Review')
const [checkpointVerifies, crossCutting] = await Promise.all([
  pipeline(
    CHECKPOINTS,
    (cp) => agent(staticPrompt(cp), { phase: 'Review', schema: REVIEW_SCHEMA, label: `review:${cp.id}` }),
    (review, cp) => agent(verifyPrompt(review, cp), { phase: 'Verify', schema: VERIFY_SCHEMA, label: `verify:${cp.id}` })
  ),
  parallel([gatesThunk, claimsThunk]),
])

const [gates, claims] = crossCutting
const verifies = checkpointVerifies.filter(Boolean)

log(`Collected ${verifies.length}/${CHECKPOINTS.length} checkpoint verdicts; gates=${gates ? gates.overall : 'null'}, claims_honest=${claims ? claims.overall_honest : 'null'}`)

// ---- synthesize ----
phase('Synthesize')
const synthesis = await agent(
  `${RULES}

You are the SYNTHESIS judge. Reconcile all evidence below into a single review of the 2026-06-27 long-tail audit-fix plan (5 checkpoints: C1 M1 retention, C2 L_subprocess, C3 L_codex_redact, C4 D2 race, C5 L_reconcile). These checkpoints claim to fix findings that fix-verification.md previously marked NOT_FIXED.

PER-CHECKPOINT VERIFY RESULTS (JSON array):
${JSON.stringify(verifies)}

CROSS-CUTTING GATES (JSON):
${JSON.stringify(gates)}

PLAN-CLAIMS AUDIT (JSON):
${JSON.stringify(claims)}

Produce the structured synthesis:
- per_checkpoint: one entry per checkpoint with a final verdict, confidence, a one-liner, the single most important piece of evidence, and any blocking issues. Trust the verify-stage verdicts but DOWNGRADE if the differential did not prove the fix or an empirical check exposed a regression (esp. C3's redact_sensitive truncation result).
- cross_cutting_summary: did architecture invariants + lifecycle hold? Call out specifically whether the new synchronous scheduler handler is invariant-safe.
- claims_audit_summary: is the plan's self-reported status (git clean, no commit/staging, file list, test counts) honest? Note anything the plan over-claimed or left unverified (e.g. full suite not run).
- overall_verdict: SHIP (all VERIFIED, gates green, claims honest) / SHIP_WITH_FIXES (real but bounded issues to address first) / DO_NOT_SHIP (a defect or failing gate).
- blockers: concrete must-fix items (with file refs) before this should be committed.
- recommended_next_steps: short, ordered.
- headline: one paragraph bottom-line for the user, in Spanish (the user writes in Spanish).
Return ONLY the structured object.`,
  { schema: SYNTH_SCHEMA, label: 'synthesize', effort: 'high' }
)

return { synthesis, verifies, gates, claims }
