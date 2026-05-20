# Validación cruzada de las 3 auditorías contra el código

**Origen:** análisis del usuario sobre `audit_sessions_2026-05-20.md`, `audit_identity_truncation_2026-05-20.md`, `audit_llm_router_2026-05-20.md`.
**Método:** grep del repo + `.venv` + `.env` + lectura de funciones clave.

---

## Críticas del usuario, validadas

### Crítica 1 — “(unused)” no fue verificado en el venv del Claude SDK
**Veredicto: tienes razón. Mi hipótesis era débil. La realidad es peor.**

Grep exhaustivo:

| Path | Match |
|---|---|
| `claw_v2/adapters/anthropic.py` | **0 ocurrencias** |
| `claw_v2/adapters/codex.py` | 0 |
| `claw_v2/adapters/openai.py` | 0 (devuelve `""` cuando no hay output) |
| `claw_v2/adapters/google.py`, `ollama.py`, `base.py` | 0 |
| `claw_v2/llm.py` | 0 |
| `claw_v2/redaction.py` | 0 |
| `claw_v2/hooks.py` | 0 |
| Todo `claw_v2/` (productivo) | **0** |
| `tests/test_final_render_idempotency.py:129` | 1 (test, no producción) |
| `.venv/lib/python3.13/site-packages/claude_agent_sdk/_bundled/claude` | 1 — pero es JavaScript ASN.1 bit-string parser (`if(tag==="bitstr"){var unused=buffer.readUInt8()…`). **No es un placeholder de respuesta.** |
| Resto del venv | solo en `pycparser`, `Cython`, `numpy`, `lxml`, `cffi`: comentarios C/h y variables internas, ninguna ruta de respuesta LLM |

**Conclusión revisada:** la cadena `"(unused)"` que aparece como `response_text` 124 veces en `observe_stream` **no se genera en el código actual**. Tres hipótesis sobrevivientes:

1. **Filas legacy:** filas escritas por una versión anterior del adapter que ya no existe. Verificable comparando `MIN(timestamp)` vs git log del `adapters/`.
2. **CLI helper bash:** scripts/* o llamadas indirectas que setean `content="(unused)"` manualmente. (No encontradas, pero existen `scripts/_cdp_*.py` que podrían serializar campos así.)
3. **El SDK CLI compilado emite stderr con `(unused)`** durante un error path, y el adapter Anthropic lo captura como `result_text`. Improbable porque el grep del binary solo matchea el código JS no-runtime, pero no descartable sin reproducir.

**Acción correcta:** antes de “sanitizar `(unused)` en `bot._send_response`” (mi recomendación B13), correr:
```sql
SELECT MIN(timestamp), MAX(timestamp) FROM observe_stream
WHERE event_type='llm_response' AND payload LIKE '%"(unused)"%';
```
Si `MAX(timestamp) < fecha_último_deploy_adapter`, el problema ya está arreglado por el redespliegue y queda solo limpiar telemetría histórica.

---

### Crítica 2 — `provider=anthropic + model=gpt-5.5` podría ser misconfig env
**Veredicto: tu hipótesis es razonable pero el `.env` la descarta. Es el bug del fallback.**

`.env` actual:
```
BRAIN_PROVIDER=anthropic
BRAIN_MODEL=claude-opus-4-7
WORKER_MODEL=claude-sonnet-4-6   (WORKER_PROVIDER omitido → default anthropic)
VERIFIER_PROVIDER=openai
VERIFIER_MODEL=gpt-5.4
RESEARCH_PROVIDER=openai
RESEARCH_MODEL=gpt-5.4
JUDGE_MODEL=claude-haiku-4-5     (JUDGE_PROVIDER omitido)
JUDGE_EFFORT=medium
```

Conflictos con la DB:

| Lane | DB dice (top 3 días) | `.env` dice | ¿Consistente? |
|---|---|---|---|
| brain | `anthropic + claude-opus-4-7` | anthropic + claude-opus-4-7 | ✅ |
| judge | `codex + gpt-5.5` (470) | model haiku, provider derivado (`critic_provider`=codex porque brain=anthropic) | ❌ model en env es haiku, DB dice gpt-5.5 |
| verifier | `anthropic + gpt-5.5` (31) | `openai + gpt-5.4` | ❌ **doble inconsistencia** |
| research | `codex + gpt-5.5` (80), `anthropic + gpt-5.5` (62) | `openai + gpt-5.4` | ❌ env apunta a openai, DB nunca lo muestra |
| worker | `anthropic + claude-sonnet-4-6` (13), `anthropic + claude-opus-4-7` (14), `anthropic + gpt-5.5` (31) | anthropic + sonnet | parcial |

**Lectura:** el `.env` que vi está **desactualizado** o el daemon corre con un env distinto. Algunos `provider=anthropic + model=gpt-5.5` (31+62 = 93 casos) sí son explicables solo por el bug del fallback (`response.provider/model` no rotan al fallback provider). Pero las inconsistencias en lane=judge/research/verifier sugieren que la config efectiva al boot del daemon es distinta del `.env` en disco.

**Acción correcta antes de tocar `_pick_fallback`:**
```bash
# en la máquina de Hector
launchctl print gui/$(id -u)/com.pachano.claw | grep -A50 EnvironmentVariables
```
Y comparar con `.env`. Si difieren, fix de config primero, luego revisar el path del fallback.

---

### Crítica 3 — “Inventa la confirmación visual” sin code path citado
**Veredicto: correcto. Es la conclusión más fuerte y la peor trazada.**

No tengo un grep que ate el último mensaje del bot (*“Verificado con screenshot del desktop — Flow está abierto y visible en tu pantalla ahora mismo”*) a una línea concreta. Lo que sí tengo:

1. **El response real fue generado por el brain** (DB: `lane=brain, provider=anthropic, model=claude-opus-4-7, ts=2026-05-20 11:50:17`).
2. **El sistema tiene `osascript` real** (claw_v2/morning_brief.py:1330+, claw_v2/computer.py:85). Sí puede ejecutar AppleScript.
3. **El detalle del response menciona “Finder open location → LaunchServices → Chrome default”** — eso es código que existe (Finder open location funciona en macOS), pero **no pude verificar que el bot lo haya ejecutado** en ese turno específico. Para validarlo necesito:
   - `SELECT * FROM observe_stream WHERE timestamp BETWEEN '2026-05-20 11:49:00' AND '2026-05-20 11:50:30' ORDER BY id;`
   - Filtrar por `event_type IN ('sdk_post_tool_use', 'sdk_post_tool_use_failure', 'tool_dispatcher')`.
   - Buscar evidencia de un Bash tool con `osascript` o `screencapture`.

**Sin esa traza, la afirmación “inventa” es plausible pero no probada.** Las alternativas (también plausibles): (a) el screenshot SÍ se tomó pero el browser que se trajo al frente era el headless invisible para Hector; (b) el bot copió texto del `last_turn_summary` del turno anterior. La queja explícita de Hector en el siguiente mensaje (*“No la veo”*) prueba la falla de UX, no el code path.

**Acción correcta:** reabrir la DB cuando se complete el checkpoint del daemon y trazar el turno. La afirmación queda como **hipótesis fuerte**, no como bug confirmado.

---

### Crítica 4 — Bajar `_MAX_STARTUP_CONTEXT_CHARS` no resuelve nada
**Veredicto: 100% correcto. Mi recomendación era ruido.**

Tamaños reales:

| Archivo | bytes | cap | uso del cap |
|---|---:|---:|---:|
| USER.md | 168 | 30.000 | 0,6 % |
| HEARTBEAT.md | 534 | 30.000 | 1,8 % |
| BOOT.md | 593 | 30.000 | 2,0 % |
| TOOLS.md | 915 | 30.000 | 3,1 % |
| CLAUDE.md | 1.842 | 30.000 | 6,1 % |
| IDENTITY.md | 2.079 | 30.000 | 6,9 % |
| BOOT_PROTOCOL.md | 2.498 | 30.000 | 8,3 % |
| AGENTS.md | 3.967 | 30.000 | 13,2 % |
| SOUL.md | 7.443 | 30.000 | 24,8 % |
| MEMORY.md | 7.750 | 30.000 | 25,8 % |
| **Total stable** | **27.789** | **300.000** (10×30K) | **9,3 %** |
| Daily memory (10 archivos) | 32.698 | 120.000 (10×12K) | 27,2 % |
| **Total** | **60.487** | | |
| System prompt observado en DB | 67.958 | 180.000 (`_MAX_STARTUP_CONTEXT_CHARS`) | **38 %** |

Cap global al 38 %, ningún archivo cerca de su cap individual. **Bajar el cap no afecta el output**: los archivos ya están muy por debajo.

**Recomendación corregida:** el peso real está en (a) las 10 dailies (32 KB combinadas) y (b) `SOUL.md` + `MEMORY.md` (15 KB). Las palancas reales:
1. **Rotar dailies:** mostrar solo la del día actual + una síntesis semanal precomputada. Ahorro: ~25 KB.
2. **Comprimir SOUL.md y MEMORY.md** — son humanos, tienen prosa redundante. Ahorro estimado: 30-40 %.
3. **Aprovechar prompt caching:** si los 60K son estables turno a turno, Anthropic cacheada al 90 %+ del prompt. El problema NO es el tamaño sino que `_clear_provider_sessions_for_app_locked` **invalida ese cache** en cada compaction.

---

### Crítica 5 — Conflicto entre auditorías: 5× más contexto vs límites Anthropic
**Veredicto: correcto. Mis dos recomendaciones eran incompatibles entre sí.**

Suma de mis propuestas:

| Cambio | Antes | Después | Δ |
|---|---:|---:|---:|
| compaction `max_messages` | 80 | 200 | 2,5× |
| `preserve_recent` | 40 | 80 | 2× |
| `_ROLLING_SUMMARY_MAX_CHARS` | 12K | 20K | 1,67× |
| `WORKER_RESULT_SUMMARY_CHARS` | 900 | 4.000 | 4,44× |
| `PHASE_INPUT_SUMMARY_CHARS` | 1.500 | 6.000 | 4× |

Con un brain ya en 18K tokens/turno + coordinator activo con 3 workers que generan input para synthesis 4× mayor, **un solo turno complejo puede escalar a 60-80K tokens de input**. Anthropic Opus tiene 200K context pero rate-limit en TPM (tokens per minute). En horas pico se entra en `rate_limit_error` → circuit breaker → fallback a OpenAI → degradado.

**Plan escalonado correcto:**
- Semana 1: cerrar B1 + sanitizar (unused) + narrow handler identidad. Telemetría limpia.
- Semana 2: subir solo `WORKER_RESULT_SUMMARY_CHARS` 900 → 2.500 y medir; subir `_ROLLING_SUMMARY_MAX_CHARS` 12K → 16K y medir.
- Semana 3: si los anteriores no rompieron nada, subir `max_messages` 80 → 150.
- Nunca aplicar todo a la vez.

---

## Lo que añades y mis auditorías omiten

### Anthropic prompt caching (5 min default / 1 h con TTL extendido)
**Tu observación es exacta y verificable.**

`claw_v2/config.py:454`:
```python
cache_prefix_ttl=_env_int("CACHE_PREFIX_TTL", 3600),
```
y `claw_v2/llm.py:104`:
```python
cache_ttl=self.config.cache_prefix_ttl if self.config.cache_prefix_ttl > 0 else None,
```
El sistema está configurado a **1 hora de cache TTL** (extended cache de Anthropic). El adapter mide cache_hit_ratio (`anthropic.py:159`) y emite evento `prompt_cache` con `estimated_savings_pct = hit_ratio × 75`.

**Tu punto es correcto y reforzado:** cuando `memory.py:563` llama `_clear_provider_sessions_for_app_locked` durante la compaction:
- borra el `provider_session_id` ↔ pierde la conversación
- el siguiente turno llega con `session_id=None` ↔ el cache prefijal de 60K chars del system prompt **se invalida** (Anthropic cachea por `(api_key, model, system_prompt_prefix)` y la sesión es un binding indirecto a través del session_id)
- el siguiente turno paga tokens completos del system prompt en lugar del ~25 % cuando hay cache hit

**Coste real:** con prompt de 17K input tokens y Opus a ~$15/$75 (input/output), una invalidación cuesta ~$0.25 extra por turno. 131 turnos perdidos = ~$30/día en cache misses evitables. Sin contar latencia.

### `current_goal` fix asume clasificador que no existe
**Veredicto: parcialmente. Existe pero está apagado por hotfix.**

`claw_v2/bot.py:5283-5297`:
```python
def _maybe_handle_task_intent(self, text: str, *, session_id: str) -> str | None:
    # HOTFIX: brittle canned task intent router over-triggers on generic
    # task-related questions and bypasses the brain. Reversible via env
    # flag CLAW_DISABLE_TASK_INTENT_ROUTER=1 (default ON until evidence-
    # aware classifier is in place).
    # TODO: replace with explicit task_id / recent-context check.
    if (os.getenv("CLAW_DISABLE_TASK_INTENT_ROUTER", "1") == "1"
        and not _has_literal_task_id(text)):
        return None
```
Hay un `_classify_task_intent` (bot.py:5270) pero **el wrapper que lo invoca está deshabilitado por default** porque sobre-disparaba. El TODO admite que hay que reemplazarlo con un clasificador “evidence-aware”.

**Tu observación queda matizada:** la fix de current_goal SÍ requiere construir o reactivar un clasificador. No es un bug que se arregle con 5 líneas; es un proyecto pequeño (estimo 2-4 horas) que toca:
- decidir cuándo un mensaje del user **es** un goal nuevo (verbo imperativo + objeto identificable) vs un goal-fragment (queja, reacción)
- escribir el clasificador o pasarlo a un LLM call barato (haiku) por turno
- testear contra los últimos 500 mensajes para evitar el over-trigger histórico

---

## Validación adicional: B1 sigue confirmado en el código

`task_ledger.py:304-316` (`mark_stale_running_lost`):
```sql
UPDATE agent_tasks
SET status = 'lost',
    completed_at = ?,
    error = 'runtime lost authoritative backing state',
    verification_status = 'failed',
    updated_at = ?
WHERE status = 'running'
  AND updated_at < ?
```
**No filtra por `task_id NOT LIKE 'brain-tooluse:%'` ni por `metadata.brain_tool_use=True`.** Mata indiscriminadamente cualquier fila `running` más vieja que el cutoff. B1 confirmado.

---

## Veredicto final sobre tu plan de ataque

Tu orden es correcto y conservador. Mi única adición:

| # | Tu paso | Estado | Nota |
|---|---|---|---|
| 1 | Cerrar ledger de brain-tooluse post-`agent_response_ready` | **Aprobado** | Cambio quirúrgico en `bot.py:_record_brain_tooluse_ledger` |
| 2 | Sanitizar `(unused)` en `bot._send_response` | **Condicional** | Antes correr la query de fechas. Si ya no se produce, solo limpiar histórico |
| 3 | Narrow regex del handler de identidad | **Aprobado** | Trivial |
| 4 | Persistir correcciones como `profile_facts` | **Aprobado, prioridad alta** | Resuelve amnesia raíz, no síntomas |
| 5 | Tunear caps con benchmark | **Aprobado** | Escalonar como en validación 5 |

**Adición:** intercalar entre 1 y 2 un paso que verifique el env real del daemon contra `.env`:
```bash
launchctl print gui/$(id -u)/com.pachano.claw | grep -E "^\s*(BRAIN|WORKER|VERIFIER|RESEARCH|JUDGE)_"
```
Si difiere del `.env`, el bug del fallback (Audit #2 §6) se invalida — sería simple config drift.

---

## Score de las 3 auditorías post-validación

| Auditoría | Bugs duros | Hipótesis | Recomendaciones útiles | Recomendaciones a corregir |
|---|---:|---:|---:|---:|
| #3 (sessions) | B1 (sólido), B6 `--json` (sólido), B5 worktree (sólido), B7 playwright (sólido) | B2 `(unused)` mal trazado, B10 healthcheck/key (sin verificar runtime) | 6/13 | B2 acción, B13 hasta no verificar fechas |
| #1 (identity) | §4 truncamiento (sólido), §6 current_goal (sólido pero requiere proyecto), §10 caching (faltaba) | §10.8 alucinación visual sin traza | 4/13 recomendaciones de §11 | §11 caps de archivos (no resuelve nada) |
| #2 (router) | §5.3 retry exclusion (sólido), §5.4 caps coordinator (sólido), §6 fallback no-rota provider (sólido) | §6 también podría ser config drift | 5/6 | Subir 5× los caps necesita escalonar |

Tu lectura cruzada es más precisa que mi última audit individual. Aceptado el plan de ataque con la adición del check de env del daemon.
