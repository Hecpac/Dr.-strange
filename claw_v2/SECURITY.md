# Security Policy

## Workspace Isolation
- Default workspace: current working directory (`Path.cwd()`)
- Agents operate within workspace unless explicitly allowlisted
- Allowlisted read paths: `/private/tmp` (configurable via `ALLOWED_READ_PATHS`)
- Extra workspace roots configurable via `EXTRA_WORKSPACE_ROOTS`
- All file tools (`Read`, `Write`, `Edit`, `Glob`, `Grep`) enforce path containment via `_require_within_workspace` — resolved paths must be under `workspace_root`

## Credential Management
- Credentials stored outside the workspace, NOT in `.env` files
- Default macOS implementation uses Keychain-backed credential scopes:
  - com.pachano.claw.researcher: GSC read-only, Analytics read-only
  - com.pachano.claw.operator: git (local), brew
  - com.pachano.claw.deployer: git push, hosting APIs, OANDA
- Never share credentials across agent classes
- No secrets in workspace directory — credential adapter retrieves at runtime

## Input Validation & Hardening
- **Path traversal**: all file operations resolve and validate against allowed roots (`tools.py`, `browser.py`, `telegram.py`, `adapters/ollama.py`)
- **Command injection**: `sandbox.py` uses `shlex.split` + token-level checking (not substring blacklists); `pipeline.py` validates branch names via strict regex `^[a-zA-Z0-9._/-]+$`
- **Package manager isolation**: `pip`, `pip3`, `npm`, `npx`, and `ensurepip` are not allowed in agent sandbox profiles
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
