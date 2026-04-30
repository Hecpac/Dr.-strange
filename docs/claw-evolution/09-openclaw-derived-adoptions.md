# P6 — Adopciones derivadas de OpenClaw 2026.4.26

Última revisión: 2026-04-28
Status: partially implemented (A and E landed; B/C/D/F remain backlog)

## Contexto

OpenClaw es un proyecto separado que **no estamos activando**. La release
[v2026.4.26](https://github.com/openclaw/openclaw/releases/tag/v2026.4.26)
trae varios fixes y patrones que vale la pena adoptar conceptualmente en
`claw_v2/` cuando lleguemos al final del Claw Evolution Plan, sin ejecutar
OpenClaw como dependencia.

Esta fase corre **después de P5 (FaR structured doubt flags)** y se trata
como hardening operativo del runtime de Claw, no como evolución cognitiva.

## Items priorizados

### A. EPIPE / broken-pipe no-fatal en envíos a Telegram

- **Problema:** `claw_v2/telegram.py` tiene `except Exception` genéricos
  que cubren broken-pipe por accidente, no por diseño. Si PTB cambia el
  shape del error, podemos perder mensajes silenciosamente o crashear.
- **Acción:** capturar `BrokenPipeError` y `ConnectionResetError`
  explícitamente en `send_text`, `send_photo`, `send_video_url` y los
  message handlers. Loguear como warning no-fatal y continuar.
- **Estado:** implementado para envíos proactivos (`send_text`, `send_photo`,
  `send_video_url`) con tests. Los handlers mantienen fallback genérico.
- **Esfuerzo:** ~10 líneas, alto leverage.

### B. Atomic install + swap para auto-updates de Claw

- **Problema:** `./scripts/restart.sh` no protege contra actualizaciones
  parciales. Si en el futuro hacemos auto-update vía `git pull` + reinstall,
  un fallo a mitad deja estado mixto.
- **Acción:** instalar nueva versión a `~/.claw/staging/<version>`, validar
  con smoke test (probe `:8765`, healthcheck), y solo entonces hacer swap
  atómico al destino final + reload del launcher.
- **Esfuerzo:** medio, requiere refactor de launcher.

### C. Profile-scoped state directory

- **Problema:** todos los archivos de Claw viven directo en `~/.claw/`.
  Si Hector eventualmente corre instancias separadas para clientes
  (TIC Insurance, SGC tenant) no hay separación.
- **Acción:** introducir `config.profile_name` (default `"default"`) y
  resolver `state_dir = ~/.claw/<profile>/`. Migración transparente para
  usuarios existentes.
- **Esfuerzo:** medio, beneficio futuro.

### D. `subagents.allowAgents` para Critic spawn (encaja con P4)

- **Problema:** cuando implementemos el async external critic (P4 del
  Claw Evolution Plan), el actor podría auto-spawnear un proceso `claude`
  que se aprueba a sí mismo — anti-patrón.
- **Acción:** definir `allowed_critic_spawners` y validar antes de
  cualquier `subprocess.Popen` de un segundo `claude`.
- **Esfuerzo:** se hace dentro de P4, no es trabajo separado.

### E. Token rotation: no-echo en streams compartidos

- **Problema:** Claw usa `TELEGRAM_BOT_TOKEN`, `RESEND_API_KEY`,
  `ANTHROPIC_API_KEY`, `HEYGEN_API_KEY`, `EXA_API_KEY` y otros.
  Si alguno se rota en runtime y la respuesta se broadcastea a observers
  o logs, expone el secret.
- **Acción:** redaction obligatoria de campos `*token*`, `*key*`, `*secret*`,
  `*password*` antes de `observe.emit` o cualquier append a JSONL/MEMORY.
- **Estado:** implementado en `redaction.py` y usado por el writer JSONL de
  telemetry; cubierto por tests.
- **Esfuerzo:** bajo, se monta sobre la redaction de P0.

### F. Realpath caching en sandbox path resolution

- **Problema:** `claw_v2/sandbox.py` resuelve paths repetidamente para
  validar workspace_root y allowlist. No es bottleneck hoy, pero sería
  útil si crece la carga.
- **Acción:** cache LRU por turno de `pathlib.Path.resolve()` keyed por
  string original.
- **Esfuerzo:** bajo, oportunista.

## Lo que NO adoptamos

- WebSocket-only Google Live (specific a OpenClaw Talk feature).
- `ALL_PROXY` Undici dispatcher (no corremos detrás de proxy).
- BlackHole 2ch para Google Meet (no usamos Meet).
- Plugin discovery via symlinks (no tenemos sistema de plugins).
- ACP runtime y subagent runtime aliases (no tenemos esa abstracción).

## Orden de ejecución

1. Item A (EPIPE) — independiente, hacer cuando se retome el Plan.
2. Item E (token redaction) — pliega con P0 cuando se implemente.
3. Item D (allowAgents) — pliega con P4 cuando se implemente.
4. Item C (profile scoping) — antes del primer cliente multi-tenant real.
5. Item B (atomic install) — cuando se implemente auto-update.
6. Item F (realpath cache) — solo si profiling lo justifica.

## Criterio de cierre

P6 se considera cerrada cuando:

- Item A está en `claw_v2/telegram.py` con tests.
- Item E está integrado al pipeline de Typed Action Events de P0.
- Items D, C, B, F están en backlog explícito o ejecutados según se necesiten.
