# Slice 2a — marca de halt persistente: evidencia de smoke en vivo

Fecha: 2026-07-06 · PR #220 → main `3f4597c` · deploy clone `~/srv/claw-daemon` pid 69518

## Contexto

Hallazgo #2 del pase de puntos ciegos 2026-07-06, reencuadrado por recon-s2: el
boot ya corre `integrity_check` (0.13s), pero un `RuntimeDatabaseError` sin
capturar moría y launchd `KeepAlive=true` lo relanzaba (~10s) saltándose
restart.sh y watchdog, sin registro persistente de por qué (la marca degraded
era process-local; el exit code del preflight se descartaba en restart.sh:74).

Slice 2a (spec congelado): marca `runtime_db_halt.json` (atómica+fsync, junto a
la DB) escrita por preflight y por `_ensure_runtime_db_boot_health`; restart.sh
honra el exit del preflight y aborta antes del kickstart + alerta; launcher en
hold-loop vivo-sin-exec mientras la marca exista; auto-clear SOLO cuando una DB
existente re-pasa integrity (rename para audit, nunca delete).

## Review (regla de verificación pre-merge)

Gemini 1 + Codex 2 + CodeRabbit 2 findings — todos evaluados y aplicados:
- **P1 (Codex)**: fallo del lado-backup con fuente sana ya NO escribe marca
  (re-verifica la fuente; test dedicado). Cazó un falso-positivo real.
- Guard de credenciales Telegram en el alert de boot (Gemini).
- Token fuera del argv de curl (`--config -` por stdin) en ambos notifiers (Codex/CodeRabbit).
- fsync de marca + directorio padre (CodeRabbit) — la marca existe para
  sobrevivir el crash window.
- Validación numérica + piso 5s de `CLAW_DB_HALT_RECHECK_S` (CodeRabbit).
Ronda final: CodeRabbit pass, 0 findings nuevos; Fast gate + secret-scan verdes.

## Deploy

Clone a `3f4597c`, restart → pid 69518, **12/12 `startup_healthcheck_ok`**,
puerto 8765, watchdog quieto, sin marca (DB sana). El propio deploy ejercitó el
camino feliz nuevo (preflight rc=0 → restart normal).

## Smoke — scratch DB, producción intocada (spec decisión #4)

Scratch: `claw.db` con bytes basura + copia sana aparte.

| Paso | Resultado |
|------|-----------|
| 1. Preflight sobre DB corrupta | **exit 1** + marca escrita (reason/error/source=preflight/created_at) + sin backup |
| 2. `restart.sh` con `DB_PATH=scratch` | **abortó antes del kickstart** ("aborting restart before kickstart", rc=1); daemon real intacto (pid 69518 antes=después) + alerta 🛑 Telegram |
| 3. Launcher con marca presente | **HOLD vivo sin exec** (12s+, ciclos de preflight cada 5s) + alerta 🛑 HOLD |
| 4. Restore de copia sana sobre scratch | siguiente ciclo pasó integrity → marca renombrada `.cleared-20260706-222648` → alerta ✅ "sale del HOLD" → `DRY_EXEC: would exec …` → **exit 0** |

Mecanismo de alertas verificado en directo: mismo curl `--config` → Telegram
API `{"ok":true}` message_id 14854. **Las 3 alertas del smoke (🛑 restart
abortado, 🛑 HOLD, ✅ sale del HOLD) + la 🧪 de prueba: recepción confirmada
por Hector (~22:40 UTC).**

## Veredicto

Arco completo **PASS**: corrupción → marca durable → restart abortado →
launcher held (KeepAlive neutralizado) → restore → auto-clear → boot liberado.
Con alerta al owner en cada transición (no_silent_degrade).

## Carriles no ejercidos en vivo (test-locked)

Camino boot-side (`_ensure_runtime_db_boot_health` → marca + alerta + re-raise)
— exigiría corromper la DB que un daemon real va a abrir; queda test-locked en
`tests/test_runtime_db_halt.py::BootHealthHaltTests` (3 tests). Fallo
backup-side sin marca: test-locked (P1 del review).
