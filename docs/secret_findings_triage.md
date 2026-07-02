# Secret Findings Triage - PR-8A

Date: 2026-06-30

Scope: local triage of `scripts/scan_secrets.py` findings. This report is redacted. It uses paths, line numbers, source sets, rule ids, classifications, and fingerprints only. No secret value is included.

## Summary

- Scan command: `.venv/bin/python scripts/scan_secrets.py`
- Scanner exit: `1`
- Total findings: 139
- Skipped files: 13342

### Total By Source Set

| Source set | Findings |
| --- | ---: |
| tracked | 33 |
| untracked_nonignored | 2 |
| ignored | 104 |

### Total By Rule Id

| Rule id | Findings |
| --- | ---: |
| anthropic_api_key_literal | 2 |
| authorization_bearer | 10 |
| fal_key_literal | 1 |
| generic_secret_assignment | 123 |
| openai_api_key_literal | 3 |

### Total By Classification

| Classification | Findings |
| --- | ---: |
| true_positive_secret | 1 |
| test_fixture_safe | 12 |
| documentation_example_safe | 17 |
| placeholder_safe | 0 |
| false_positive_rule_noise | 108 |
| unknown_requires_owner_review | 1 |

## Critical True Positives

| Path | Line | Source set | Rule id | Fingerprint | Classification | Recommended action |
| --- | ---: | --- | --- | --- | --- | --- |
| `scripts/_seedance_fal.py` | 6 | ignored | fal_key_literal | sha256:72588a9230efbb86ee3c4f3999d7a0ce7ea0f9925225415a2a3ca8cb36227eed | true_positive_secret | Remove literal, migrate to env/Keychain, and rotate manually out of band if the key was valid. |

## Release Blockers

- `scripts/_seedance_fal.py:6` is a true positive FAL key literal. Release remains blocked until the literal is removed and manual rotation is handled outside the repo if applicable.
- `scripts/_heygen_avatar_create2.py:4` is an ignored local asset id captured as `generic_secret_assignment`. It is not confirmed as a credential, but requires owner review before release/handoff.
- CI will fail on tracked findings until safe test/doc fixtures and internal key-name constants are rewritten, scoped, or otherwise handled.
- Local release/handoff will fail on ignored vendor/artifact/report findings until PR-8B defines a narrow remediation strategy. Do not add broad suppressions.
- The untracked `.patch-backups/...` findings are false-positive constants, but the file is untracked and nonignored. It should be either intentionally tracked, ignored, or removed in a separate authorized cleanup.

## Tracked Findings

| Path | Lines | Rule id(s) | Classification | Rationale |
| --- | --- | --- | --- | --- |
| `claw_v2/adapters/base.py` | 27 | generic_secret_assignment | false_positive_rule_noise | Internal metadata key constant, not a credential. |
| `claw_v2/daemon.py` | 23 | generic_secret_assignment | false_positive_rule_noise | Internal resume key constant, not a credential. |
| `claw_v2/scheduled_background_jobs.py` | 15, 17, 19, 21, 23, 25, 27, 29, 31, 34, 36, 38, 40 | generic_secret_assignment | false_positive_rule_noise | Internal resume key constants, not credentials. |
| `claw_v2/skill_expand_jobs.py` | 15 | generic_secret_assignment | false_positive_rule_noise | Internal resume key constant, not a credential. |
| `claw_v2/verification/local_tool_runner.py` | 39, 40, 41, 42 | generic_secret_assignment | false_positive_rule_noise | Internal result/artifact key constants, not credentials. |
| `claw_v2/verification/promote_gate.py` | 45, 46 | generic_secret_assignment | false_positive_rule_noise | Internal artifact key constants, not credentials. |
| `docs/superpowers/specs/2026-03-23-claw-pending-items-design.md` | 264 | openai_api_key_literal | documentation_example_safe | Documentation placeholder/example, not a usable credential. |
| `tests/test_anthropic.py` | 53 | authorization_bearer | test_fixture_safe | Synthetic command fixture. |
| `tests/test_anthropic_auth_mode.py` | 93, 102 | anthropic_api_key_literal | test_fixture_safe | Synthetic auth-mode fixtures. |
| `tests/test_approval.py` | 29 | generic_secret_assignment | test_fixture_safe | Synthetic diff fixture. |
| `tests/test_approval_gate.py` | 201 | generic_secret_assignment | test_fixture_safe | Synthetic diff summary fixture. |
| `tests/test_bot_helpers.py` | 63 | authorization_bearer | test_fixture_safe | Synthetic redaction fixture. |
| `tests/test_computer_import_safety.py` | 148 | openai_api_key_literal | test_fixture_safe | Synthetic import safety fixture. |
| `tests/test_f3b2_heygen_provider_readonly.py` | 28, 29 | generic_secret_assignment | test_fixture_safe | Synthetic provider/approval fixtures. |
| `tests/test_redaction.py` | 19 | authorization_bearer | test_fixture_safe | Synthetic redaction fixture. |

## Untracked Findings

| Path | Lines | Rule id(s) | Classification | Rationale |
| --- | --- | --- | --- | --- |
| `.patch-backups/remediation-20260623-050130/pr1-c4-promote-gate.patch` | 76, 77 | generic_secret_assignment | false_positive_rule_noise | Patch backup contains internal artifact key constants, not credentials. Still risky as untracked nonignored material. |

## Ignored Findings

| Path | Lines | Rule id(s) | Classification | Rationale |
| --- | --- | --- | --- | --- |
| `.venv/lib/python3.13/site-packages/authlib/oauth2/rfc6749/resource_protector.py` | 116 | authorization_bearer | documentation_example_safe | Dependency OAuth example/spec text. |
| `.venv/lib/python3.13/site-packages/authlib/oauth2/rfc6750/parameters.py` | 18 | authorization_bearer | documentation_example_safe | Dependency OAuth example/spec text. |
| `.venv/lib/python3.13/site-packages/browser_use/browser/demo_mode.py` | 20, 21, 22, 25 | generic_secret_assignment | false_positive_rule_noise | Dependency storage key constants, not credentials. |
| `.venv/lib/python3.13/site-packages/browser_use/telemetry/service.py` | 32 | generic_secret_assignment | false_positive_rule_noise | Vendored telemetry project key constant, not a repo credential. |
| `.venv/lib/python3.13/site-packages/google/api_core/gapic_v1/client_info.py` | 24 | generic_secret_assignment | false_positive_rule_noise | Dependency metadata key constant. |
| `.venv/lib/python3.13/site-packages/google/api_core/gapic_v1/routing_header.py` | 27 | generic_secret_assignment | false_positive_rule_noise | Dependency metadata key constant. |
| `.venv/lib/python3.13/site-packages/google/api_core/page_iterator.py` | 322 | generic_secret_assignment | false_positive_rule_noise | Dependency token field name constant. |
| `.venv/lib/python3.13/site-packages/google/api_core/version_header.py` | 15 | generic_secret_assignment | false_positive_rule_noise | Dependency metadata key constant. |
| `.venv/lib/python3.13/site-packages/google/auth/environment_vars.py` | 103, 104 | generic_secret_assignment | false_positive_rule_noise | Dependency environment variable name constants. |
| `.venv/lib/python3.13/site-packages/google/auth/metrics.py` | 30, 31 | generic_secret_assignment | false_positive_rule_noise | Dependency request type constants. |
| `.venv/lib/python3.13/site-packages/google/genai/client.py` | 321 | generic_secret_assignment | documentation_example_safe | Dependency documentation/example text. |
| `.venv/lib/python3.13/site-packages/google/genai/tests/conftest.py` | 99 | generic_secret_assignment | test_fixture_safe | Vendored dependency test fixture. |
| `.venv/lib/python3.13/site-packages/google_genai-1.68.0.dist-info/METADATA` | 152 | generic_secret_assignment | documentation_example_safe | Dependency package metadata example. |
| `.venv/lib/python3.13/site-packages/mcp/shared/experimental/tasks/helpers.py` | 32, 35 | generic_secret_assignment | false_positive_rule_noise | Dependency metadata key constants. |
| `.venv/lib/python3.13/site-packages/oauthlib/oauth2/rfc6749/clients/base.py` | 185 | authorization_bearer | documentation_example_safe | Dependency OAuth example/spec text. |
| `.venv/lib/python3.13/site-packages/oauthlib/oauth2/rfc6749/tokens.py` | 191 | authorization_bearer | documentation_example_safe | Dependency OAuth example/spec text. |
| `.venv/lib/python3.13/site-packages/openai-2.30.0.dist-info/METADATA` | 117 | openai_api_key_literal | documentation_example_safe | Dependency package metadata example. |
| `.venv/lib/python3.13/site-packages/posthog/test/test_utils.py` | 18 | generic_secret_assignment | test_fixture_safe | Vendored dependency test fixture. |
| `.venv/lib/python3.13/site-packages/pydantic/mypy.py` | 82, 83 | generic_secret_assignment | false_positive_rule_noise | Dependency config/metadata key constants. |
| `.venv/lib/python3.13/site-packages/pydantic/v1/class_validators.py` | 48, 49 | generic_secret_assignment | false_positive_rule_noise | Dependency validator config key constants. |
| `.venv/lib/python3.13/site-packages/pydantic/v1/mypy.py` | 77, 78 | generic_secret_assignment | false_positive_rule_noise | Dependency config/metadata key constants. |
| `artifacts/hyperframes/node_modules/@google/genai/README.md` | 130 | generic_secret_assignment | documentation_example_safe | Vendored package documentation example. |
| `artifacts/hyperframes/node_modules/hono/dist/cjs/helper/ssg/middleware.js` | 31 | generic_secret_assignment | false_positive_rule_noise | Vendored package header key constant. |
| `artifacts/hyperframes/node_modules/hono/dist/helper/ssg/middleware.js` | 4 | generic_secret_assignment | false_positive_rule_noise | Vendored package header key constant. |
| `artifacts/hyperframes/node_modules/hono/dist/types/helper/ssg/middleware.d.ts` | 4 | generic_secret_assignment | false_positive_rule_noise | Vendored package header key constant. |
| `artifacts/hyperframes/node_modules/jwa/index.js` | 7, 8, 9 | generic_secret_assignment | false_positive_rule_noise | Vendored package error message constants. |
| `artifacts/pachano-hyperframes/node_modules/@google/genai/README.md` | 130 | generic_secret_assignment | documentation_example_safe | Vendored package documentation example. |
| `artifacts/pachano-hyperframes/node_modules/hono/dist/cjs/helper/ssg/middleware.js` | 31 | generic_secret_assignment | false_positive_rule_noise | Vendored package header key constant. |
| `artifacts/pachano-hyperframes/node_modules/hono/dist/helper/ssg/middleware.js` | 4 | generic_secret_assignment | false_positive_rule_noise | Vendored package header key constant. |
| `artifacts/pachano-hyperframes/node_modules/hono/dist/types/helper/ssg/middleware.d.ts` | 4 | generic_secret_assignment | false_positive_rule_noise | Vendored package header key constant. |
| `artifacts/pachano-hyperframes/node_modules/jwa/index.js` | 7, 8, 9 | generic_secret_assignment | false_positive_rule_noise | Vendored package error message constants. |
| `artifacts/remotion-grid/node_modules/@remotion/player/dist/cjs/volume-persistance.js` | 5 | generic_secret_assignment | false_positive_rule_noise | Vendored package storage key constant. |
| `artifacts/remotion-grid/node_modules/@remotion/player/dist/esm/index.mjs` | 2854 | generic_secret_assignment | false_positive_rule_noise | Vendored package storage key constant. |
| `artifacts/remotion-grid/node_modules/@remotion/renderer/dist/combine-chunks.js` | 33 | generic_secret_assignment | false_positive_rule_noise | Vendored package internal token constant. |
| `artifacts/remotion-grid/node_modules/@remotion/renderer/dist/esm/index.mjs` | 14865, 22693 | generic_secret_assignment | false_positive_rule_noise | Vendored package internal token constants. |
| `artifacts/remotion-grid/node_modules/@remotion/renderer/dist/offthread-video-server.js` | 38 | generic_secret_assignment | false_positive_rule_noise | Vendored package internal token constant. |
| `artifacts/remotion-grid/node_modules/@remotion/studio/dist/components/QuickSwitcher/algolia-search.js` | 5 | generic_secret_assignment | false_positive_rule_noise | Vendored package public search key constant; not a repo credential. |
| `artifacts/remotion-grid/node_modules/remotion/dist/cjs/delay-render.d.ts` | 2, 4, 5 | generic_secret_assignment | false_positive_rule_noise | Vendored package internal token constants. |
| `artifacts/remotion-grid/node_modules/remotion/dist/cjs/delay-render.js` | 15, 17, 18 | generic_secret_assignment | false_positive_rule_noise | Vendored package internal token constants. |
| `artifacts/remotion-grid/node_modules/remotion/dist/cjs/input-props-serialization.d.ts` | 8, 9 | generic_secret_assignment | false_positive_rule_noise | Vendored package serialization token constants. |
| `artifacts/remotion-grid/node_modules/remotion/dist/cjs/input-props-serialization.js` | 6, 7 | generic_secret_assignment | false_positive_rule_noise | Vendored package serialization token constants. |
| `artifacts/remotion-grid/node_modules/remotion/dist/esm/index.mjs` | 460, 461, 1654, 1656, 1657 | generic_secret_assignment | false_positive_rule_noise | Vendored package token constants. |
| `artifacts/remotion-grid/node_modules/remotion/dist/esm/no-react.mjs` | 146, 148, 149, 152, 153 | generic_secret_assignment | false_positive_rule_noise | Vendored package token constants. |
| `artifacts/steipete_stack/steipete__bslog_README.md` | 132, 185, 659 | generic_secret_assignment | documentation_example_safe | Vendored README examples. |
| `prototypes/programmatic-seo/node_modules/any-promise/loader.js` | 3 | generic_secret_assignment | false_positive_rule_noise | Vendored package registration key constant. |
| `prototypes/programmatic-seo/node_modules/next/dist/build/preview-key-utils.js` | 22, 23 | generic_secret_assignment | false_positive_rule_noise | Vendored package internal preview key constants. |
| `prototypes/programmatic-seo/node_modules/next/dist/build/preview-key-utils.js.map` | 1 | generic_secret_assignment | false_positive_rule_noise | Vendored source map for internal preview key constants. |
| `prototypes/programmatic-seo/node_modules/next/dist/esm/build/preview-key-utils.js` | 7, 8 | generic_secret_assignment | false_positive_rule_noise | Vendored package internal preview key constants. |
| `prototypes/programmatic-seo/node_modules/next/dist/esm/build/preview-key-utils.js.map` | 1 | generic_secret_assignment | false_positive_rule_noise | Vendored source map for internal preview key constants. |
| `prototypes/programmatic-seo/node_modules/next/dist/esm/server/app-render/encryption-utils-server.js` | 11 | generic_secret_assignment | false_positive_rule_noise | Vendored package internal encryption key constant. |
| `prototypes/programmatic-seo/node_modules/next/dist/esm/server/app-render/encryption-utils-server.js.map` | 1 | generic_secret_assignment | false_positive_rule_noise | Vendored source map for internal encryption key constant. |
| `prototypes/programmatic-seo/node_modules/next/dist/esm/server/lib/router-utils/build-prefetch-segment-data-route.js` | 5 | generic_secret_assignment | false_positive_rule_noise | Vendored package segment path key constant. |
| `prototypes/programmatic-seo/node_modules/next/dist/esm/server/lib/router-utils/build-prefetch-segment-data-route.js.map` | 1 | generic_secret_assignment | false_positive_rule_noise | Vendored source map for segment path key constant. |
| `prototypes/programmatic-seo/node_modules/next/dist/server/app-render/encryption-utils-server.js` | 26 | generic_secret_assignment | false_positive_rule_noise | Vendored package internal encryption key constant. |
| `prototypes/programmatic-seo/node_modules/next/dist/server/app-render/encryption-utils-server.js.map` | 1 | generic_secret_assignment | false_positive_rule_noise | Vendored source map for internal encryption key constant. |
| `prototypes/programmatic-seo/node_modules/next/dist/server/lib/router-utils/build-prefetch-segment-data-route.d.ts` | 1 | generic_secret_assignment | false_positive_rule_noise | Vendored package segment path key constant. |
| `prototypes/programmatic-seo/node_modules/next/dist/server/lib/router-utils/build-prefetch-segment-data-route.js` | 36 | generic_secret_assignment | false_positive_rule_noise | Vendored package segment path key constant. |
| `prototypes/programmatic-seo/node_modules/next/dist/server/lib/router-utils/build-prefetch-segment-data-route.js.map` | 1 | generic_secret_assignment | false_positive_rule_noise | Vendored source map for segment path key constant. |
| `reports/2026-05-31/followup_audit_2026-05-31.md` | 931, 4621 | authorization_bearer, generic_secret_assignment | documentation_example_safe | Ignored audit report contains synthetic/example snippets. |
| `reports/2026-06-10/auditoria_integral_2026-06-10.md` | 2060, 3703, 3705, 3707, 3709 | generic_secret_assignment | false_positive_rule_noise | Ignored audit report contains copied internal key constants. |
| `reports/2026-06-10/auditoria_integral_2026-06-10.md` | 4444, 4522 | authorization_bearer | documentation_example_safe | Ignored audit report contains synthetic/example redaction snippets. |
| `scripts/_heygen_avatar_create2.py` | 4 | generic_secret_assignment | unknown_requires_owner_review | Ignored local script contains an uploaded asset id stored in a variable named `KEY`; owner must confirm sensitivity. |
| `scripts/_seedance_fal.py` | 6 | fal_key_literal | true_positive_secret | Ignored local script contains a FAL key literal. |
| `scripts/_test_deep_research.py` | 13 | generic_secret_assignment | false_positive_rule_noise | Parser condition on environment variable names, not a literal credential. |

## False-Positive Candidates

| Path/group | Rule id | Reason |
| --- | --- | --- |
| `claw_v2/*`, `claw_v2/verification/*` internal constants | generic_secret_assignment | Names end in `_KEY`, but values are metadata/resume/artifact field names, not credentials. |
| `.patch-backups/.../pr1-c4-promote-gate.patch` | generic_secret_assignment | Patch backup repeats internal artifact key constants. |
| `.venv/lib/python3.13/site-packages/**` dependency constants | generic_secret_assignment | Vendored package metadata/config/token field names. |
| `artifacts/**/node_modules/**` dependency constants | generic_secret_assignment | Vendored package storage, preview, serialization, or error-message constants. |
| `prototypes/**/node_modules/**` dependency constants | generic_secret_assignment | Vendored Next.js/internal package constants. |
| `reports/**` copied internal constants | generic_secret_assignment | Historical audit snippets, not active credentials. |
| `scripts/_test_deep_research.py` | generic_secret_assignment | String-prefix parser for env variable names, not an assignment of a secret. |

## PR-8B Recommendation

1. Remove the FAL key literal from `scripts/_seedance_fal.py`, migrate usage to environment/Keychain, and handle manual rotation out of band if the key was valid.
2. Review `scripts/_heygen_avatar_create2.py` with the owner. If the asset id is sensitive or user-linked, move it to local env/config outside the repo; otherwise convert the variable name so it no longer looks like a credential.
3. Rewrite tracked test fixtures and the tracked documentation placeholder so the scanner no longer reports them, preferably by composing sensitive-looking strings in tests or using non-detectable placeholders.
4. Refine scanner handling for internal constants with narrowly tested rules, for example excluding known internal field-name suffixes only when the matched value is a non-secret identifier.
5. Decide a narrow policy for ignored vendored dependencies and generated artifacts. Avoid broad suppressions; prefer fingerprint/path scoped exceptions or operational cleanup of generated dependency trees.
6. Decide the fate of `.patch-backups/...`: ignore, remove, or intentionally track under a separate checkpoint. Do not let it remain untracked/nonignored before release.

## PR-8B Update

- `scripts/_seedance_fal.py` no longer contains a FAL key literal. It now requires `FAL_KEY` from the environment or local secret manager.
- `scripts/_heygen_avatar_create2.py` no longer stores the uploaded asset id literal. It now requires `HEYGEN_IMAGE_ASSET_ID` from the environment.
- Tracked test fixtures and the tracked documentation example were rewritten so they do not look like stored provider credentials.
- `.secret-scan-allowlist.json` contains only scoped suppressions by exact path, rule id, and fingerprint for findings classified as `test_fixture_safe`, `documentation_example_safe`, or `false_positive_rule_noise`.
- There are no allowlist entries for `true_positive_secret`, `unknown_requires_owner_review`, or `fal_key_literal`.
- Post-remediation scanner summary: `scan_exit=0`, `findings=0`, `suppressed=126`, `skipped=13345`.

Release note: if the removed FAL literal was valid, key rotation remains a manual out-of-band release blocker. No rotation was performed in PR-8B.
