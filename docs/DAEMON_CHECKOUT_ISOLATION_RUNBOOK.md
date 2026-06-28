# Daemon checkout isolation (P0-1, Design B) — runbook

**Status: LIVE since 2026-06-28.** Root-cause fix for the 2026-06-28 strand (an
external Warp/Oz terminal agent ran raw `git checkout -b` + `cherry-pick` in the
*shared* checkout and moved the daemon's HEAD). The daemon now runs from a
dedicated clone with an independent `.git`, so human/terminal git operations
cannot reach the daemon's HEAD. #156 branch-integrity remains as detection; this
is prevention.

## Topology

- **Daemon clone — `~/srv/claw-daemon` (DEPLOY-ONLY).** Runs the 24/7 daemon.
  Independent `.git`. Owns at runtime: `claw_v2/` code, `memory/` (in-vivo
  commits), `reports/`, `data/claw.db` (+WAL, `observation_window.json`),
  `.venv` (uv). `origin` → `https://github.com/Hecpac/Dr.-strange.git`.
  launchd `com.pachano.claw` → `~/srv/claw-daemon/ops/claw-launcher.sh`.
- **Human/dev clone — `~/Projects/Dr.-strange`.** For Warp / Oz / Claude Code /
  manual git. Safe for any git op; its HEAD is decoupled from the daemon
  (proven: a human `git checkout -b` here does **not** move the daemon's HEAD).

## Rules

1. **All interactive git / terminal-agent work → `~/Projects/Dr.-strange` only.**
   Never hand-run git in `~/srv/claw-daemon`; it is deploy-only (ff-only).
2. **Deploy to the daemon** (from `~/srv/claw-daemon`):
   `git fetch origin && git merge --ff-only origin/main && bash scripts/restart.sh`.
3. **Restart the daemon:** `bash ~/srv/claw-daemon/scripts/restart.sh`, or from
   anywhere `launchctl kickstart -k gui/501/com.pachano.claw` (the launchd label
   is bound to the clone plist, so this is correct from either clone).
4. **`think` CLI:** target the clone DB explicitly —
   `python -m claw_v2.cli.think --db ~/srv/claw-daemon/data/claw.db ...`.
   The cwd-relative default (`data/claw.db`) reads the *frozen* human-clone DB
   when run from `~/Projects/Dr.-strange`.
5. **Quiesce / maintenance:** set `CLAW_MAINTENANCE_MODE=1` in `~/.claw/env` +
   restart → claim OFF, scheduler OFF, drain OFF. Remove + restart to resume.
   (`CLAW_AUTONOMOUS_MAINTENANCE_ENABLED` does **not** gate job claiming.)

## launchd jobs (gui/501)

Repointed to the clone (daemon-coupled — run `claw_v2` code / read `claw.db`):
`com.pachano.claw`, `com.pachano.claw-watchdog`,
`com.pachano.claw-post-turn-id-audit`.

Left pointing at the human clone (auxiliary; no daemon DB/code coupling — the
human clone retains its scripts + `.venv`, so they keep working):
`com.claw.chrome-cdp` (not loaded), `com.claw.vercel-watchdog`,
`com.pachano.vercel-watchdog`, `com.pachano.heygen-deliver-once`.

## Cutover artifacts / rollback

- Backups (in human clone): consistent DB
  `data/claw.db.bak-pre-p01-isolation-20260628T175639Z`; plist copies
  `~/Library/LaunchAgents/com.pachano.claw{,-watchdog,-post-turn-id-audit}.plist.bak-pre-p01-20260628T175639Z`.
- **Rollback:** `launchctl bootout gui/501/com.pachano.claw` → copy
  `~/srv/claw-daemon/data/claw.db` (newest) back to
  `~/Projects/Dr.-strange/data/` → restore the plist from `.bak` →
  `launchctl bootstrap gui/501 <plist>`; re-bootstrap the watchdog with the old
  path. Net safety = the `.backup` above.

## Residuals (acceptable / future)

- `EXTRA_WORKSPACE_ROOTS=/Users/hector/Projects` still grants the daemon
  read/write to the human clone. This is **not** the strand vector (which was an
  external agent moving the *daemon's* HEAD, now prevented by separate `.git`);
  tighten only if desired.
- The human clone's `data/claw.db` is now frozen at the pre-cutover snapshot —
  do not read it as live state.
- An optional non-blocking `post-checkout` hook on the daemon clone could warn on
  off-main HEAD; physical isolation + #156 already cover this. Do **not** use a
  `reference-transaction` hook (fires on every ref-update — own brick surface).
