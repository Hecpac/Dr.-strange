# Security Policy

## Workspace Isolation
- Default workspace: ~/claw_workspace
- Agents operate within workspace unless explicitly allowlisted
- Allowlisted read paths: ~/Projects (for code inspection)
- Allowlisted write paths: none outside workspace by default
- Enforced via PreToolUse hooks + optional OS-level sandbox hardening

## Credential Management
- Credentials stored outside the workspace, NOT in `.env` files
- Default macOS implementation uses Keychain-backed credential scopes:
  - com.pachalo.claw.researcher: GSC read-only, Analytics read-only
  - com.pachalo.claw.operator: git (local), npm, brew
  - com.pachalo.claw.deployer: git push, hosting APIs, OANDA
- Never share credentials across agent classes
- No secrets in workspace directory — credential adapter retrieves at runtime

## Content Safety
- All web/email/document content passes through sanitizer PostToolUse hook
- Researcher agents can read web but cannot mutate
- Operator/Deployer agents receive only sanitized summaries of external content
- Quarantine extraction uses Structured Outputs (strict: true) — API-level guarantee

## MCP Server Allowlist
- Only listed servers may be loaded; unaudited servers are blocked at startup
- In-process servers (preferred):
  - claw-tools v2.1.6 (custom semantic tools)
  - claw-eval-mocks v2.1.6 (hermetic eval adapters)
- External servers (require version pin + monthly audit):
  - (none currently — add here if needed, with pinned version and SHA256)
- Audit schedule: monthly review of all servers for new advisories

## Escalation
- Any suspicious content pattern → log + alert user
- Any sandbox policy violation → block + alert user
- 3 consecutive sandbox violations by an agent → auto-demote to Tier 1
- Any MCP server advisory → immediate review, patch or remove
