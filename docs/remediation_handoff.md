# Remediation Handoff

Date: 2026-06-30

## Executive Status

Technical gates are green in the local tree:

- Focal remediation gate: PASS
- Full `tests/` suite: PASS
- Root `pytest -q`: PASS
- Secret scanner: PASS
- Diff hygiene: PASS
- Index/staging: empty

Operational blockers remain outside the repository workflow:

- Review or rotate the prior `FAL_KEY` value if it was a valid credential.
- Review or rotate the prior HeyGen value if it was sensitive.
- Decide whether the remediated ignored scripts stay local-only or need tracked safe templates.
- Decide what to do with `.patch-backups/`.

No restart, migration, staging, commit, secret rotation, cleanup, or external action was performed during the remediation checkpoints.

## Files Expected In The PR

### Modified Tracked Files

These files are already tracked and contain the accumulated remediation changes:

- `claw_v2/SECURITY.md`
- `claw_v2/capability_grants.py`
- `claw_v2/computer.py`
- `claw_v2/computer_gate.py`
- `claw_v2/computer_handler.py`
- `claw_v2/config.py`
- `claw_v2/jobs.py`
- `claw_v2/main.py`
- `claw_v2/model_registry.py`
- `claw_v2/sqlite_runtime.py`
- `claw_v2/task_handler.py`
- `claw_v2/tools.py`
- `docs/superpowers/specs/2026-03-23-claw-pending-items-design.md`
- `pyproject.toml`
- `tests/test_anthropic.py`
- `tests/test_anthropic_auth_mode.py`
- `tests/test_approval.py`
- `tests/test_approval_gate.py`
- `tests/test_bot_helpers.py`
- `tests/test_capability_grants.py`
- `tests/test_computer.py`
- `tests/test_computer_diagnostics.py`
- `tests/test_computer_gate.py`
- `tests/test_computer_import_safety.py`
- `tests/test_config.py`
- `tests/test_f3b2_heygen_provider_readonly.py`
- `tests/test_f4_delegation.py`
- `tests/test_jobs.py`
- `tests/test_model_registry.py`
- `tests/test_redaction.py`
- `tests/test_sqlite_runtime.py`
- `tests/test_task_handler.py`
- `tests/test_tools.py`

Rationale by group:

- Browser security: canonical PDP, exact approval scopes, browser gate shims, and browser-use enforcement.
- Job lifecycle: formal cancel APIs and legacy `cancel()` fail-closed under formal leases.
- Runtime DB: owner-level degraded mode, structured reasons, and healthcheck behavior.
- Automation outcomes: structured success contract and task handler integration.
- Traversal/tools: budgeted glob/grep, streaming reads, telemetry, and symlink/root containment.
- Secret hygiene: documentation, scanner wiring, fixture rewrites, and pytest collection hygiene.

### Untracked Files To Include Later

These are new files that should be staged intentionally if this remediation is prepared as a PR:

- `.github/workflows/secret-scan.yml`
- `.secret-scan-allowlist.json`
- `claw_v2/automation_contracts.py`
- `claw_v2/automation_outcome.py`
- `claw_v2/automation_policy.py`
- `claw_v2/secret_scanning.py`
- `claw_v2/workspace_traversal.py`
- `docs/secret_findings_triage.md`
- `docs/remediation_handoff.md`
- `docs/superpowers/specs/2026-06-29-f4-automation-orchestrator-design.md`
- `scripts/scan_secrets.py`
- `tests/test_automation_contracts.py`
- `tests/test_automation_outcome.py`
- `tests/test_automation_policy.py`
- `tests/test_secret_scanning.py`
- `tests/test_workspace_traversal.py`

Rationale by group:

- `.github/workflows/secret-scan.yml`: CI wiring for local secret scanner.
- `.secret-scan-allowlist.json`: exact path, rule, and fingerprint suppressions for triaged safe findings.
- `claw_v2/automation_*`: browser PDP and structured outcome contracts.
- `claw_v2/secret_scanning.py` and `scripts/scan_secrets.py`: reproducible scanner and CLI.
- `claw_v2/workspace_traversal.py`: shared traversal service for Glob/Grep.
- `docs/*`: triage, F4 design record, and final handoff manifest.
- `tests/*`: red/green contracts for the remediated behavior.

## Files Not Expected In The PR

### Ignored Local Scripts

These scripts were remediated locally so the workspace scanner is clean, but they remain ignored and will not travel in a PR unless an owner makes a separate packaging decision:

- `scripts/_seedance_fal.py`
- `scripts/_heygen_avatar_create2.py`

Current local state:

- `scripts/_seedance_fal.py` no longer contains a `FAL_KEY` literal and requires the value from environment or local secret manager.
- `scripts/_heygen_avatar_create2.py` no longer contains the plausible literal and requires `HEYGEN_IMAGE_ASSET_ID` from environment.

Recommended follow-up:

- Keep them local-only, or create tracked safe templates such as `scripts/seedance_fal.example.py` and `scripts/heygen_avatar_create.example.py` in a separate checkpoint.

### Local Artifacts And Tool State

Ignored local files and directories observed by Git include local agent/tool state and generated artifacts such as:

- `.adal/`
- `.agents/`
- `.augment/`
- `.claude/`
- `.codebuddy/`
- `.commandcode/`
- `.continue/`
- `artifacts/`
- `reports/`
- `renders/`
- `generated_images/`

These are not PR inputs. The secret scanner still scans ignored files locally where Git lists them, but broad artifact cleanup was intentionally not performed.

### Owner Decision Required

`.patch-backups/` is untracked and not expected in the PR without explicit owner decision:

- `.patch-backups/remediation-20260623-050130/pr1-c4-promote-gate.patch`
- `.patch-backups/remediation-20260623-050130/pr2-browser-tools-security.patch`
- `.patch-backups/remediation-20260623-050130/pr3-watchdog-smoke.patch`
- `.patch-backups/remediation-20260623-050130/pr4-internal-wiring.patch`

No cleanup was performed.

## Security Status

Latest accepted scanner state:

- `scan_exit=0`
- `findings=0`
- `suppressed=126`
- `skipped=13350`

Allowlist status:

- Entries: 124
- Wildcard/global suppressions: 0
- Missing classification/reason: 0
- Forbidden class suppressions: 0
- `fal_key_literal` suppressions: 0
- Entries suppressing multiple findings: 2

The two multi-suppression entries are exact path, rule, and fingerprint suppressions for duplicated documentation/example findings:

- `artifacts/steipete_stack/steipete__bslog_README.md`, `generic_secret_assignment`, count 2.
- `reports/2026-06-10/auditoria_integral_2026-06-10.md`, `authorization_bearer`, count 2.

Rotation and review blockers:

- `FAL_KEY`: review or rotate manually if the removed literal was valid.
- HeyGen value: review or rotate manually if the removed value was sensitive.

The repository does not automate key rotation.

## Verification Commands

Run these from the repository root:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/scan_secrets.py
git diff --check
git diff --cached --name-only
```

Last accepted results:

- `pytest -q`: `4011 passed, 1 skipped, 16 warnings, 539 subtests passed`
- Secret scanner: `scan_exit=0`, `findings=0`, `suppressed=126`, `skipped=13350`
- `git diff --check`: PASS
- `git diff --cached --name-only`: no output

## Reviewer Notes

Checkpoint summary:

- PR-0A: local red reproduction tests.
- PR-0B: mergeable containment for HIGH browser actions and formal legacy cancel fail-closed.
- PR-1: full browser PDP with exact ApprovalScope and adapter/grant tightening.
- PR-2: formal job cancellation APIs.
- PR-3: RuntimeDb degraded mode v1.
- PR-4: AutomationOutcome and evidence-based browser task success.
- PR-5: WorkspaceTraversalService for bounded Glob/Grep.
- PR-6: local secret scanner.
- PR-7: secret scan CI/local wiring and release gate documentation.
- PR-8A: redacted secret finding triage.
- PR-8B: secret remediation and exact allowlist suppressions.
- PR-9: final integration audit.
- PR-10A: full-suite convergence and root pytest collection hygiene.
- PR-10B: handoff manifest.

Known warnings:

- `ToolContractWarning` in existing tool contract tests.
- Telegram `PTBDeprecationWarning` for `retry_after`.

Root pytest behavior:

- `pyproject.toml` sets `testpaths = ["tests"]`.
- This prevents `pytest -q` from collecting generated/local artifacts such as `artifacts/pipeline_test.py`, which previously attempted to connect to local PostgreSQL and raised during collection.
- `artifacts/` was not edited.
