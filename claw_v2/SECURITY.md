# Security Policy

## Workspace Isolation
- Default workspace: current working directory (`Path.cwd()`)
- Agents operate within workspace unless explicitly allowlisted
- Allowlisted read paths: `$HOME`, `/private/tmp` (configurable via `ALLOWED_READ_PATHS`)
- Extra workspace roots configurable via `EXTRA_WORKSPACE_ROOTS`
- All file tools (`Read`, `Write`, `Edit`, `Glob`, `Grep`) enforce path containment via `_require_within_workspace` — resolved paths must be under `workspace_root`

## Credential Management
- Credentials stored outside the workspace, NOT in `.env` files
- Default macOS implementation uses Keychain-backed credential scopes:
  - com.pachano.claw.researcher: GSC read-only, Analytics read-only
  - com.pachano.claw.operator: git (local), npm, brew
  - com.pachano.claw.deployer: git push, hosting APIs, OANDA
- Never share credentials across agent classes
- No secrets in workspace directory — credential adapter retrieves at runtime

## Secret Scanning Gate
- Never place secrets in tracked, untracked, or ignored workspace files.
- Local scan command: `.venv/bin/python scripts/scan_secrets.py`
- Exit codes: `0` means clean, `1` means findings were detected, `2` means scanner execution/configuration failed.
- Findings are redacted and include path, line number, source set, rule id, and fingerprint. Do not paste or store raw secret values when triaging findings.
- `scripts/_*.py` files are scanned, including ignored local scripts.
- Release gate: release requires a clean scan or a reviewed exception ticket with path, rule id, fingerprint, owner, and remediation plan.
- Scanner allowlist entries must be exact path + rule id + fingerprint suppressions with classification and reason; never suppress true positives or unknown owner-review findings.
- If a `FAL_KEY` literal appears, treat the key as compromised and rotate it manually out of band. Rotation is not automated from this repository.
- CI runs `scripts/scan_secrets.py` without credentials or external providers. CI covers files present in checkout; ignored local files are only covered when they exist in the job, so run the local scanner before release.

## Input Validation & Hardening
- **Path traversal**: all file operations resolve and validate against allowed roots (`tools.py`, `browser.py`, `telegram.py`, `adapters/ollama.py`)
- **Command injection**: `sandbox.py` uses `shlex.split` + token-level checking (not substring blacklists); `pipeline.py` validates branch names via strict regex `^[a-zA-Z0-9._/-]+$`
- **Race conditions**: `approval.py` uses `fcntl` file locking for atomic read-modify-write; `network_proxy.py` and `cron.py` use `threading.Lock` for shared state
- **JS injection**: `browser.py` escapes all user input via `json.dumps` before embedding in JS templates; browser names validated via `^[a-zA-Z0-9_-]+$`
- **Screenshot path traversal**: `browser.py` strips directory components from filenames via `Path(name).name`
- **Image path restriction**: `adapters/ollama.py` restricts `_encode_image` to `/tmp` and `$HOME`
- **Config resilience**: `_env_int` / `_env_float` helpers return safe defaults on malformed env vars instead of crashing

## Content Safety
- All web/email/document content passes through sanitizer PostToolUse hook
- Researcher agents can read web but cannot mutate
- Operator/Deployer agents receive only sanitized summaries of external content
- Quarantine extraction uses Structured Outputs (strict: true) — API-level guarantee

## MCP Server Allowlist
- Only listed servers may be loaded; unaudited servers are blocked at startup
- In-process servers (preferred):
  - claw-tools (custom semantic tools)
  - claw-eval-mocks (hermetic eval adapters)
- External servers (require version pin + monthly audit):
  - (none currently — add here if needed, with pinned version and SHA256)
- Audit schedule: monthly review of all servers for new advisories

## Local Audit Model
- Gemma 4 E4B via Ollama for $0 code auditing on Mac 16GB
- Modelfile versioned at `claw_v2/models/code-auditor.Modelfile`
- Build: `ollama create code-auditor -f claw_v2/models/code-auditor.Modelfile`
- Config: temp=0.3, num_ctx=8192, strict anti-hallucination system prompt
- Validated: 23/23 files audited, 48 real issues, 0 hallucinations

## Escalation
- Any suspicious content pattern → log + alert user
- Any sandbox policy violation → block + alert user
- 3 consecutive sandbox violations by an agent → auto-demote to Tier 1
- Any MCP server advisory → immediate review, patch or remove
