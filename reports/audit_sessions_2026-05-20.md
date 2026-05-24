# Auditoría — últimas sesiones del bot (Dr. Strange / Claw)

**Ventana auditada:** 2026-05-17 → 2026-05-20 11:55 UTC
**Fuente:** `data/claw.db` (observe_stream 262.7K eventos, agent_tasks 196 filas, messages 186, task_outcomes 2.6K)
**Sesión activa:** `tg-574707975` (Telegram)
**Provider actual del brain:** `anthropic / claude-opus-4-7` (efectivo en últimos turnos)

---

## TL;DR — qué está pasando

1. **131 de 136 tareas `brain-tooluse:*` terminan en `status=lost` / `verification_status=failed`** con el mismo error: `runtime lost authoritative backing state`. Eso es un bug, no una caída real — la respuesta al usuario sí salió (`agent_response_ready` posterior al `agent_event_received`).
2. **El verifier nunca cierra el ciclo.** Las tareas brain-tooluse quedan `running + needs_verification` y el watchdog (`daemon._reconcile_stale_tasks` → `task_ledger.mark_stale_running_lost`) las mata a los 300s.
3. **El adapter Anthropic está intermitente:** `Stream idle timeout - partial response received` y `Claude SDK execution failed` aparecen en `llm_error`. Cuando cae, dispara fallback a OpenAI.
4. **El fallback OpenAI está emitiendo basura:** respuestas con texto `to=multi_tool_use.parallel 天天中彩票软件 …` (13 ocurrencias). Es contaminación del response (probable model-tag bleed) que **no se sanitiza** antes de auditar/responder.
5. **Browser/CDP automation rota** en varios frentes: `browser_cli.py: error: unrecognized arguments: --json`, Playwright sin browsers (`/tmp/pw-browsers/chromium_headless_shell-1208/...`), selectores Flow caducados (`model_btn: {found: False}`), URLs erradas (`https://2026---91227.xlsx`, `https://guiainstalacion.md`).
6. **Daemon tick con bug de tipo:** `'<' not supported between instances of 'float' and 'NoneType'` recurrente.
7. **Self-improve worktree spam:** `git worktree add … exp-1 returned non-zero exit status 128` porque el directorio ya existe.
8. **8 approvals colgadas** en `pending_approval_ids` sin drenar.
9. **`OPENAI_API_KEY no está configurado`** en healthcheck (degraded) pero el router sigue derivando llamadas a OpenAI como fallback — contradicción.

---

## Bugs encontrados (por severidad)

### B1 — CRÍTICO · Brain tool-use jamás cierra el task_ledger row
**Síntoma:** `agent_tasks` con `status=lost`, `error='runtime lost authoritative backing state'`, `verification_status=failed`, `provider/model = NULL`, `mode=brain_fallback`. 131 ocurrencias en últimos 3 días sobre `tg-574707975`.

**Causa raíz:**
- `bot.py:3946-4051` (`_record_brain_tooluse_ledger`) crea la fila con `status='running'`, luego llama `task_ledger.mark_running_checkpoint(...)` con `verification_status='needs_verification'` y **retorna sin promoverla a `succeeded`**. El comentario lo dice: *“succeeded remains reserved for passed evidence”*.
- No existe ningún consumer que ejecute el verifier sobre estas filas. La función `mark_running_checkpoint` deja `status='running'`.
- `daemon.py:76` cada 300s ejecuta `task_ledger.mark_stale_running_lost(older_than_seconds=300)` → `task_ledger.py:291-325`. Esto fuerza `status='lost'` y `error='runtime lost authoritative backing state'` para cualquier fila `running` más vieja que el TTL. Está pensado para crashes, pero aquí se dispara sobre filas vivas.

**Archivos:**
- `claw_v2/bot.py:3886-4063` (creación + checkpoint del row)
- `claw_v2/task_ledger.py:291-325` (`mark_stale_running_lost`)
- `claw_v2/daemon.py:66-83` (`_reconcile_stale_tasks`)
- `claw_v2/main.py:1103-1114` (`_task_lifecycle_watchdog_handler` — sólo llama `reconcile_false_successes`, no el watchdog de stale)

**Impacto:** ledger envenenado, dashboards de salud reportan 96% de fallos falsos, reentrenamiento offline tomaría estos como ejemplos negativos.

**Fix sugerido (mínimo):**
- Distinguir filas `brain_tooluse` (metadata.brain_tool_use=True) en `mark_stale_running_lost`: marcarlas como `completed_unverified` o cerrarlas explícitamente cuando llega el `agent_response_ready` correlacionado por trace_id.
- O añadir un closer en `bot.py` tras `agent_response_ready`: si el row sigue en `running + needs_verification`, marcarlo `succeeded_unverified` (status nuevo) o `completed_unverified` con `verification_status=needs_verification`.

---

### B2 — CRÍTICO · Verifier lane devuelve `response_text="(unused)"` con `confidence=0.0`
**Síntoma:** 31 de 93 respuestas en `lane=verifier` traen `response_text="(unused)"` y `confidence=0.0` (33%). Mismo patrón en `lane=research` (62 ocurrencias) y `lane=worker` (31).

**Causa probable:**
- La cadena `"(unused)"` no existe en el código de `claw_v2/` ni en adapters — parece venir literalmente de la respuesta del proveedor (codex/anthropic) o de un placeholder en el adapter al fallar el parseo. En las muestras la `prompt_size.system_prompt_chars=0` y `effective_input_chars=709` — system prompt vacío para el verifier, lo que sugiere que el adapter no inyecta el prompt esperado.
- `llm.py:35` define `NON_TOOL_LANES = ("verifier","research","judge")`. Estas lanes no usan tool-capable adapter; si el provider tiene `system_prompt_chars=0`, está mandando un prompt mutilado.

**Archivos a revisar:** `claw_v2/llm.py:55-199`, `claw_v2/adapters/codex.py`, `claw_v2/adapters/anthropic.py` (búsqueda de “(unused)” no devolvió match en el repo Python → confirmar que no sea texto literal del adapter; probable: viene del provider y el adapter lo deja pasar).

**Impacto:** ninguna verificación real corre para esas tareas; el ciclo brain→verifier→close queda mudo. Toda fila `needs_verification` queda colgada (alimenta B1).

---

### B3 — ALTO · Fallback OpenAI inyecta texto basura (`multi_tool_use.parallel` + caracteres CJK)
**Síntoma:** 13 eventos `llm_fallback` cuyo `response_text` empieza con:
```
to=multi_tool_use.parallel  天天中彩票软件{"tool_uses":[…]}to=multi_tool_use.parallel  天天中彩票APP…
```
**Causa probable:** El fallback (Anthropic→OpenAI en `llm.py:244-247`) hace pasar el texto a `redact_sensitive` pero no sanitiza el formato. Es contaminación del modelo (eco de tags internos de tool-use de OpenAI). No estás protegiendo contra response shapes ajenas al esquema de tu pipeline.

**Archivo:** `claw_v2/llm.py:155-182`.

**Impacto:** texto basura puede llegar a Telegram (lo registramos en audit_sink antes del send-side scrubber, así que el blast radius depende del bot.py downstream).

**Fix sugerido:** detector regex `to=multi_tool_use\.parallel` → marcar `response.content = "fallback_corrupt"`, `degraded_mode=True`, y descartar el turno (re-prompt o devolver mensaje genérico).

---

### B4 — ALTO · Daemon tick crash: `'<' not supported between instances of 'float' and 'NoneType'`
**Síntoma:** `daemon_tick_error` con ese mensaje, repetido.

**Causa probable:** un comparador (probable `if updated_at < cutoff` o similar) recibe `updated_at=None`. Candidato directo: `task_ledger.mark_stale_running_lost` o algún job del scheduler donde `next_run_at` puede ser `None`.

**Acción:** grep `< cutoff` y `< now` en `daemon.py`, `task_ledger.py`, `agent_jobs` queries.

---

### B5 — ALTO · `git worktree add` para self-improve falla con exit 128
**Síntoma:** `daemon_tick_error: git worktree add --detach ~/.claw/agents/_worktrees/self-improve/exp-1 HEAD returned non-zero exit status 128`.

**Causa:** el path ya existe (worktree no limpia entre runs).

**Archivos:** ver `claw_v2/main.py` self-improve setup, `auto_research` orquestador.

**Fix:** check `Path.exists()` antes; si existe, `git worktree remove --force` o usar `exp-{ulid}` para garantizar unicidad.

---

### B6 — ALTO · `browser_cli.py` rompe por flag no soportado
**Síntoma:**
```
browser_cli.py: error: unrecognized arguments: --json {"type":"screenshot","target_id":"B83377591D76671DB7EE31ECE92CFBBB",...}
```
**Causa:** `claw_v2/browser_cli.py:18-24` sólo acepta `payload` (positional) o stdin. Algún script (o el agent loop) le pasa `--json <payload>`.

**Archivo:** `claw_v2/browser_cli.py` y el caller (revisar `scripts/cdp_*.py`, `claw_v2/chrome_handler.py`, `claw_v2/browse_handler.py`).

**Fix:** añadir `--json` como alias del positional, o eliminar el call-site que lo emite mal.

---

### B7 — MEDIO · Playwright sin browsers instalados
**Síntoma:** `Executable doesn't exist at /tmp/pw-browsers/chromium_headless_shell-1208/chrome-headless-shell-mac-arm64/chrome-headless-shell`.

**Causa:** el venv no corrió `playwright install chromium`. Toda automación headless falla y cae a CDP del Chrome del user (que también está a veces caído según los `flow_*` errors).

**Fix:** runbook de boot del daemon → `playwright install chromium`.

---

### B8 — MEDIO · Scripts ad-hoc en `scripts/flow_*.py` con selectores caducados
**Síntoma:**
- `model_btn: {'found': False}` en `scripts/flow_open_and_shoot.py` → el DOM de Flow cambió.
- `_tmp_cdp_aistudio.py` con websocket timeout porque el `TAB` id se hardcodea.
- Varios `_tmp_*.py` y `flow_state_shot.py` con tracebacks.

**Impacto:** el flujo “Abre Google Flow”/“crear prototipo” se rompe en producción del último turno (mensaje del user: *“Lo que quiero es que dejes de mentir y hagas las cosas como te las pido”*). Coincide.

**Fix:** dejar el control de Flow al MCP de Chrome en vez de scripts caseros con selectores estáticos.

---

### B9 — MEDIO · `browse` lane intenta URLs malformadas
**Síntoma:** `browse error: no se pudo leer https://2026---91227.xlsx`, `https://guiainstalacion.md`, `https://synabun.ai`, `https://t.co/pMCS05HAhB`.
- Las dos primeras son **fragmentos de markdown/texto interpretados como URLs**. El parser de URLs del browser handler las acepta.
- `t.co/xxx` (Twitter shortener) y `synabun.ai` son DNS-fail / redirect-issue.

**Archivo:** `claw_v2/browse_handler.py` — falta validación previa (regex shape: dominio TLD válido, no `.xlsx`/`.md` como TLD).

---

### B10 — MEDIO · Healthcheck degraded por `OPENAI_API_KEY` pero el router lo usa de fallback
**Síntoma:** `startup_healthcheck_degraded: OPENAI_API_KEY no está configurado; el backend openai fallará al ejecutar acciones reales`.

**Pero:** `llm.py:244` mapea `anthropic → openai` como fallback, y los logs muestran fallbacks emitiéndose. O la key sí existe en runtime y el healthcheck está leyendo otro contexto, o los fallbacks están corriendo en degraded sin key (improbable).

**Fix:** unificar la lectura — si el adapter OpenAI no tiene credential válida, `_pick_fallback` debe excluirlo y devolver `None`.

---

### B11 — BAJO · Dispatcher en `bot.py:handle_text` exhausta los 15 handlers sin match en cada turno
**Síntoma:** 6 dispatch_decisions seguidos con `reason=*_no_match` en el mismo instante (`2026-05-20 11:49:27`), luego el flujo cae al brain. No es un bug per se — es la cascada esperada por el orden canónico del INTERNAL_WIRING — pero ojo:
- Si los `*_no_match` se loguean uno por uno en cada turno, infla `observe_stream` (12.929 dispatch_decision en 3 días).

**Fix:** consolidar en un único `dispatch_decision` por turno con `tried_handlers: [list]` y la decisión final.

---

### B12 — BAJO · Frase de identidad re-prompted como respuesta del bot
**Síntoma:** *“Soy Dr. Strange en el daemon local de Hector. Claude, Codex, OpenAI y ChatGPT son proveedores o herramientas locales, no mi identidad. Retomo desde el runtime: …”* aparece como respuesta a 2 mensajes inocuos del user (`"Opción. 1"`, `"Ok de los proyectos que otro repo …"`).

**Causa probable:** match espurio en un handler de “identidad” del dispatcher. Mensajes neutros no deberían dispararlo.

**Archivo:** revisar el handler de identidad/boot_context en `bot.py:handle_text` (orden 1-15 según `INTERNAL_WIRING.md §5.1`).

---

### B13 — BAJO · Frase “La tarea X todavía necesita un paso más. (unused)”
**Síntoma:** Mensajes salientes al user con literal `(unused)` (heredados del `response_text` del verifier B2). 4 en sequencia consecutiva.

**Causa:** cadena B2 → llega a `response.content` → se cuela al canal de mensajes sin sanitizar.

**Fix:** sanitización en `bot._send_response` (o equivalente): si `content in {"(unused)", ""}`, no enviar.

---

## Archivos conectados (mapa de la cadena de fallo)

| Componente | Archivo | Rol en el bug |
|---|---|---|
| Telegram ingest → dispatch | `claw_v2/bot.py` | `handle_text`, `_record_brain_tooluse_ledger` |
| Tool-use ledger | `claw_v2/task_ledger.py` | `mark_stale_running_lost`, `mark_running_checkpoint` |
| Daemon scheduler | `claw_v2/daemon.py` | `_reconcile_stale_tasks`, `tick()` |
| Watchdog runner | `claw_v2/main.py` | `_task_lifecycle_watchdog_handler` |
| Brain/verifier router | `claw_v2/llm.py` | `ask()`, `_pick_fallback`, NON_TOOL_LANES |
| Adapters | `claw_v2/adapters/anthropic.py`, `codex.py`, `openai.py` | timeout, `(unused)` placeholder, fallback corrupto |
| Verifier infra | `claw_v2/hooks.py`, `claw_v2/plan_gate.py`, `claw_v2/task_handler.py` | `_maybe_run_petri_verifier` |
| Browse | `claw_v2/browse_handler.py`, `claw_v2/browser.py`, `claw_v2/browser_cli.py` | flag `--json`, URLs malformadas |
| Chrome / CDP | `claw_v2/chrome.py`, `claw_v2/chrome_handler.py`, `scripts/cdp_*.py`, `scripts/flow_*.py` | selectores, tab IDs hardcoded |
| Lifecycle | `claw_v2/lifecycle.py` | reconocimiento `brain-tooluse:` prefix |
| Task completion | `claw_v2/task_completion.py` | `validate_completion`, evidencia brain_fallback |
| Morning brief | `claw_v2/morning_brief.py` | filtros `INTERNAL_RUNTIMES`, `INTERNAL_TASK_ID_PREFIXES` |
| Self-improve | `claw_v2/main.py` + `auto_research/` | worktree exp-1 colisión |
| Aerial pipeline | `data/failures.jsonl` | `aerial_fetch tile_fetch_returned_none` parcel `TAR-XXX` |

---

## Señales numéricas (últ 3 días)

| Métrica | Valor |
|---|---|
| `agent_tasks.status='lost'` (mode=brain_fallback, provider=NULL) | **131 / 196** |
| `verification_status='failed'` | 142 / 196 |
| `verification_status='passed'` | 18 |
| `daemon_tick_error` | 5.918 eventos |
| `sdk_post_tool_use_failure` | 1.610 |
| `llm_error` (AdapterError) | 484 |
| `llm_fallback` total | 906 (13 con basura `multi_tool_use.parallel`) |
| `verifier llm_response="(unused)"` | 31 / 93 (33%) |
| `dispatch_decision` (cascada handlers) | 12.929 |
| Pending approvals colgadas | 8 |
| `aerial_fetch tile_fetch_returned_none` | 5 (parcel TAR-XXX) |

---

## Recomendaciones priorizadas

1. **(B1) Cerrar el ciclo brain-tooluse → ledger** antes que el watchdog mate la fila. Mínimo viable: en `bot.py` post-`agent_response_ready`, llamar `task_ledger.mark_terminal(status="completed_unverified", verification_status="needs_verification")` para filas de origen `brain_fallback`.
2. **(B2) Bloquear emisión al user de `response_text="(unused)"`** y abrir tracker del adapter que lo produce (anthropic-side, probable system_prompt vacío → empty completion).
3. **(B3)** Detector + descarte de respuestas con `multi_tool_use.parallel` en `llm.py:_pick_fallback`.
4. **(B4)** `grep '< cutoff' '< now' '< self.'` en `daemon.py`/`task_ledger.py` y guardar None.
5. **(B5)** Limpiar `~/.claw/agents/_worktrees/self-improve/` o usar nombres únicos.
6. **(B6)** Aceptar `--json` en `browser_cli.py` (o arreglar caller).
7. **(B7)** `playwright install chromium` en `pyproject`/postinstall.
8. **(B11/B12)** Auditar el handler de identidad — el `Soy Dr. Strange…` se dispara en mensajes neutros.

---

## Cómo verificar el fix (gates)

- `SELECT COUNT(*) FROM agent_tasks WHERE error='runtime lost authoritative backing state' AND created_at > <fix_ts>` debe ser **0**.
- `SELECT COUNT(*) FROM observe_stream WHERE event_type='llm_response' AND lane='verifier' AND payload LIKE '%"(unused)"%'` debe quedar **plano**.
- `SELECT COUNT(*) FROM observe_stream WHERE event_type='llm_fallback' AND payload LIKE '%multi_tool_use.parallel%'` debe ser **0**.
- `daemon_tick_error` < 10/día.
- `pending_approvals` no debería crecer sin un consumer humano.
