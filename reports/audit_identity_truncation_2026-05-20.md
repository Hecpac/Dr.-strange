# Auditoría — por qué el bot alucina y pierde identidad

**Sesión auditada:** `tg-574707975`
**Fecha:** 2026-05-20
**Pregunta de Hector:** *“por qué se desconecta de quién es y truncando cada decisión”*

---

## Veredicto rápido

El bot tiene un **system prompt gigante (~17.000 tokens)** y un **user prompt diminuto (~1.000 tokens)** donde casi no entra historial. La conversación real vive en la sesión del proveedor (Anthropic SDK), que **se borra** cuando pasa cualquiera de estos tres triggers: compaction (>80 mensajes), expiración a 2 horas, o cambio forzado por el bot. Cuando se borra, el modelo tiene que reconstruirte de un `rolling_summary` con cap de **12.000 chars y snippets de 260 chars por mensaje** — ahí pierde el dueño de cada repo, qué proyecto importa, y cuál fue tu última decisión. La identidad está reforzada en muchos contratos del prompt, pero el `current_goal` se reescribe con el último texto que mandaste (limpio o no), y un handler de dispatcher mal calibrado dispara el monólogo *“Soy Dr. Strange en el daemon local…”* en mensajes neutros.

---

## 1. Tamaños y proporciones (turno real medido en DB)

Último turno en `lane=brain`, `provider=anthropic`, `model=claude-opus-4-7`:

| Componente | Chars | Tokens estim. | % del total |
|---|---:|---:|---:|
| **system_prompt** | 67.958 | 16.989 | **94 %** |
| user prompt (turno actual) | 4.028 | 1.007 | 5,6 % |
| evidence_pack | 267 | 66 | 0,4 % |
| **Total input** | 72.253 | 18.062 | 100 % |

El bot dedica el 94 % de su ventana a instrucciones estáticas y < 6 % al diálogo del turno. **Casi ninguna conversación reciente entra como user message** — depende de la memoria interna del provider session.

---

## 2. Cómo se construye el system prompt (los 17K tokens)

**Archivos:** `claw_v2/workspace.py:198-360` (`AgentWorkspace.startup_context`), `claw_v2/main.py:1373-1400`.

El system prompt se compone de:

1. **Bloque “Startup Context”** (~12 líneas: PID, cwd, git_dirty, code_version, boot_protocol_version, reglas de identidad).
2. **STABLE_CONTEXT_FILES — 10 archivos del workspace** (workspace.py:201-212):
   `BOOT_PROTOCOL.md, SOUL.md, IDENTITY.md, USER.md, AGENTS.md, CLAUDE.md, BOOT.md, HEARTBEAT.md, TOOLS.md, MEMORY.md`. Tamaño total local: **27.789 bytes**.
   Cap por archivo: **30.000 chars** (`_MAX_CONTEXT_CHARS_PER_FILE`).
3. **Daily memory** (`memory/YYYY-MM-DD.md`): 10 archivos, **32.698 bytes** totales. Cap por archivo: **12.000 chars** (`_MAX_DAILY_CONTEXT_CHARS_PER_FILE`).
4. **`config_section`**, **`memory_sections`** (facts + learning), **`task_ledger_section`**.
5. Cap global del system prompt: **180.000 chars** (`_MAX_STARTUP_CONTEXT_CHARS`). Si lo supera, agrega `[... startup context truncated]`.
6. Sobre eso, `brain._brain_system_prompt` (brain.py:1202-1213) concatena 8 contratos más:
   `BRAIN_RESPONSE_CONTRACT`, `CONVERSATIONAL_STYLE_CONTRACT`, `BRAIN_PUSHBACK_CONTRACT`, `SELF_HEALING_LOOP_CONTRACT`, `AUTONOMY_EXECUTION_CONTRACT`, `RUNTIME_OPERATIONS_CONTRACT`, `CAPABILITY_DENIAL_CONTRACT`, `IDENTITY_ANCHOR`.

**Resultado:** el modelo recibe SOUL, IDENTITY, BOOT, MEMORY, daily notes, contratos x8 — más de 17K tokens — antes de leer una sola palabra de la conversación real.

**Impacto:**
- Atención diluida: la identidad y los hechos críticos (qué repo es tuyo, qué prototipo está vivo) están enterrados.
- Cuando el provider session se reinicia, esos 17K tokens se vuelven a procesar pero la **conversación NO** — porque la conversación vive en otra parte.

---

## 3. Cómo se construye el user prompt (los 1K tokens)

**Archivo:** `claw_v2/brain.py:471-582` (`_build_prompt`).

En el turno actual el user prompt contiene:
- `<confidence_calibration>` (delta predicho vs real para tu task_type)
- Bloques `<learned_lesson>` recuperados por similitud
- `<untrusted_context source="wiki">` con 3-5 entradas wiki (sim≥0.6)
- Bloque `# Autonomy contract` (mode, workstream, current_goal, reglas)
- Bloque `# Runtime capability context` (Chrome CDP, browser, desktop, terminal — disponibilidad)
- Finalmente: `# User request\n<texto del user>`

**Lo que NO entra como user prompt por defecto:**
- `include_history=False` es la rama común. Entonces NO se inyectan mensajes previos.
- Solo entra un “catchup” (`_build_catchup`) con mensajes que el SDK pudo haber perdido por shortcuts.

**El historial real vive en la `provider_session` (Anthropic SDK).** El bot le pasa un `provider_session_id` (ej. `2bb76a58-f264-...`) y el SDK guarda la conversación de su lado.

---

## 4. La verdadera causa de la “amnesia”: provider session se borra

**Archivo:** `claw_v2/memory.py:485-570` (`store_message`, `compact_session_messages`) y `:1054-1080` (provider_sessions, max_age=7200s).

Hay **3 triggers** que tiran a la basura toda la memoria conversacional del provider:

### 4.1 Compaction por mensajes (>80 en una sesión)
```
preserve_recent=40, max_messages=80
```
Cuando una sesión Telegram pasa de 80 mensajes, los más viejos se borran y se reducen a un `rolling_summary` con:
- snippet de **260 chars por mensaje**
- entrada total cap de **2.600 chars por bloque “Compacted N older messages…”**
- rolling_summary total cap de **12.000 chars** (`_ROLLING_SUMMARY_MAX_CHARS`)

Y **además** se llama `_clear_provider_sessions_for_app_locked(session_id)` (memory.py:563) → **el SDK del proveedor pierde la conversación entera**. El siguiente turno arranca con solo el `rolling_summary` (truncado y sintetizado).

**Tu evidencia en DB:**
- `session_state.rolling_summary` actual = **11.996 chars** (a 4 chars del cap → la próxima compaction descarta el inicio).
- En el rolling_summary se leen literalmente bloques como *“Compacted 42 older messages from 2026-05-19 17:24:13 to 2026-05-19 17:52:00.”*
- Cada uno de esos 42 mensajes quedó comprimido a un snippet de 260 chars o menos. Las decisiones finas se pierden.

### 4.2 Expiración a 2 horas (`max_age_seconds=7200`)
**Archivo:** `claw_v2/memory.py:1054-1080`.

Si pasas 2 horas sin escribir, el provider_session se considera viejo y se borra. La próxima respuesta del modelo no recordará tu conversación de la mañana — solo lo que esté en `rolling_summary`.

### 4.3 Reset explícito por el bot
**Archivo:** `claw_v2/memory.py:566` (`_mark_provider_session_reset_locked(reason="memory_compaction", summary_only_context=True)`).

El bot puede forzar el reset cuando rota proveedor (anthropic↔openai), tras una compaction, o al cambiar de session_id derivada.

---

## 5. `rolling_summary` — la única memoria que sobrevive — está truncado por el final más antiguo

**Archivo:** `claw_v2/memory.py:277-282`:
```python
def _append_rolling_summary(existing, entry):
    combined = f"{existing}\n\n{entry}" if existing else entry
    if len(combined) <= _ROLLING_SUMMARY_MAX_CHARS:  # 12_000
        return combined
    trimmed = combined[-(_ROLLING_SUMMARY_MAX_CHARS - 28):].lstrip()
    return "[older summary trimmed]\n" + trimmed
```
Cuando llega al cap, **borra el inicio** y conserva el final. Eso significa que los hechos fundacionales (el repo de tu sitio, quién es el dueño, qué proyecto NO es tuyo) son los primeros en irse cuando la conversación crece.

**Cita textual de tu rolling_summary actual**:
> *“Ya habiamod hablado que PHD no es mi proyecto, porque perdiste el contexto que ya habíamos hablado anteriormente”*
> *“Ya te había dicho cuál es el repo de mi pagina”*

Ambas correcciones tuyas SÍ están en el summary, pero como texto plano dentro de “Compacted N older messages” — el modelo las tiene que reinterpretar cada turno. Lo que NO está es la conclusión durable (“el repo es Hecpac/hector-services-site”) como fact persistente.

---

## 6. `current_goal` se sobreescribe en cada turno con el texto del user

**Archivo:** `claw_v2/bot.py:6762` (imperative handler):
```python
self.brain.memory.update_session_state(
    session_id,
    mode="ops",
    current_goal=last_user_goal[:280],
    ...
)
```
Y en el handler genérico, `bot.py:2207` también: `current_goal=objective[:280]`.

**Evidencia en DB:** `current_goal = "No haz abierto ninguna página aún"` — eso NO es un goal, es una queja del user de hace 30 segundos. El bot la persiste como objetivo y la inyecta en el `Autonomy contract` del próximo turno, contradiciendo la conversación real (vimos el screenshot que sí estaba abierto).

**Impacto:** el bot se contradice porque sigue un “goal” que es ruido conversacional, no un objetivo persistente.

---

## 7. `mode` cambia por turno, reescribiendo el contrato de autonomía

Para `tg-574707975` vimos `mode` alternando entre `'chat'`, `'coding'`, `'ops'`, `'browse'` en distintos turnos. El bloque `Autonomy contract` que se inyecta en el user prompt depende de `mode` (brain.py:548 `_autonomy_contract`). Cambiar de mode entre turnos consecutivos = cambiar las reglas del juego sin que el modelo lo justifique. Resultado: respuestas inconsistentes.

---

## 8. Por qué se cuela el monólogo *“Soy Dr. Strange en el daemon local”*

**Evidencia:** dispara incluso ante mensajes neutros como `"Opción. 1"` y `"Ok de los proyectos que otro repo …"`.

**Causa probable:** uno de los 15 handlers del dispatcher (`bot.py:handle_text`) tiene un trigger regex de “identidad/boot_context” demasiado amplio. Cuando matchea, devuelve una respuesta fija. El user lo percibe como “se olvidó de la conversación” porque literalmente abandona el contexto del turno y emite el preamble.

**Fix sugerido:** subir el umbral del handler de identidad — sólo dispararlo cuando el mensaje pregunta literalmente *quién eres / qué modelo eres / cuál es tu daemon*; nunca como reacción a “Opción 1” o “OK”.

---

## 9. Por qué llega texto basura al chat (`(unused)`, `to=multi_tool_use.parallel`)

- `(unused)`: la lane `verifier`/`research`/`worker` devolvió `response_text="(unused)"` con `confidence=0.0` 124 veces en 3 días. El string no existe en el código Python del repo — viene del adapter Anthropic cuando el system_prompt llega vacío al verifier (`system_prompt_chars=0` confirmado en las muestras DB). El bot lo serializa al chat sin sanitizar: *“La tarea X todavía necesita un paso más. (unused)”* salió al usuario 4 turnos seguidos.
- `to=multi_tool_use.parallel  天天中彩票软件 …`: contaminación del fallback OpenAI (13 ocurrencias). El detector regex `_INTERNAL_TOOL_TRACE_PATTERNS` en `brain.py:89-94` SÍ lo captura — pero el flujo de `llm_fallback` en `llm.py:155-182` lo audita ANTES de pasar por los post-hooks del brain.

---

## 10. Por qué *“alucina” o no completa la tarea*

Síntesis de la cadena causal:

1. **Truncamiento conversacional:** la conversación vive en provider_session y se borra cada 80 mensajes / 2 horas / on-reset. El modelo no tiene tu diálogo, solo el `rolling_summary` resumido.
2. **`rolling_summary` recorta lo más viejo primero** → pierdes hechos fundacionales (tu repo, tu proyecto activo).
3. **No hay un “fact store” durable de decisiones** — cuando le confirmas algo (“el repo es X”), no se persiste como `profile_fact` automáticamente, solo queda flotando en `messages` hasta la próxima compaction.
4. **`current_goal` es ruido**: se rellena con tu último texto, no con un goal real.
5. **`mode` baila**: las reglas de autonomía cambian de turno a turno.
6. **Verifier roto** (lane devolviendo “(unused)”): las filas brain-tooluse nunca se cierran como `succeeded`, el ledger las marca `lost` a los 5 minutos → el bot cree que “falló” y se reanima en `brain_fallback` perdiendo más contexto.
7. **Fallback OpenAI contaminado**: cuando Anthropic falla por timeout, OpenAI mete texto basura que el sanitizador no atrapa siempre.
8. **Scripts de browser/CDP rotos** (selectores de Flow, Playwright sin browser, `--json` no reconocido) → cuando intenta verificar “está abierto” usando esos scripts, falla, y para no quedarse callado **inventa la confirmación visual** (la del último turno: *“Flow está abierto y visible”* — porque el shot real lo tomaba un Chrome headless invisible para ti).

---

## 11. Configuración exacta que necesitas tocar

| Parámetro | Archivo:línea | Valor actual | Recomendado |
|---|---|---|---|
| `_MAX_STARTUP_CONTEXT_CHARS` | workspace.py:16 | 180.000 | 60.000 (recortar agresivamente) |
| `_MAX_CONTEXT_CHARS_PER_FILE` | workspace.py:14 | 30.000 | 6.000 (resumen, no copia) |
| `_MAX_DAILY_CONTEXT_CHARS_PER_FILE` | workspace.py:15 | 12.000 | 3.000 |
| `_ROLLING_SUMMARY_MAX_CHARS` | memory.py:18 | 12.000 | 20.000 + estrategia FIFO selectiva (no truncar hechos marcados como `durable`) |
| `_COMPACTED_MESSAGE_SNIPPET_CHARS` | memory.py:17 | 260 | 600 + flag para no truncar mensajes del **user** |
| `max_messages` compaction | memory.py:492-523 | 80 | 200 (mucho menos agresivo) |
| `preserve_recent` | memory.py:493 | 40 | 80 |
| `provider_sessions max_age_seconds` | memory.py:1055 | 7.200 (2 h) | 86.400 (24 h) o cero (no expirar por tiempo) |
| `_clear_provider_sessions` en compaction | memory.py:563 | siempre | **no limpiar** si el provider es Anthropic con caching (prefix cache) |
| `current_goal` overwrite | bot.py:2207, 6762 | `last_user_goal[:280]` | solo escribir cuando el handler clasifica el mensaje como objetivo nuevo |
| `mode` per-turn | bot.py varios | rotación libre | bloquear rotación sin signal explícito; persistir el mode previo |
| Identity handler trigger | bot.py:handle_text (un handler entre los 15) | regex amplio | exigir intent literal “¿quién eres / qué modelo eres?” |
| evidence_pack contenido | brain.py | 267 chars / 66 tokens | inyectar últimas 5-10 decisiones del task_ledger y 3 facts top |

---

## 12. Qué hacer YA (3 cambios que cortan el 80 % del dolor)

1. **Persistir decisiones del user como `profile_facts` automáticamente.** Cuando Hector dice *“el repo es X”* / *“mi proyecto es Y”* / *“PHD no es mío”*, el bot debe convertirlo en un fact durable (clave: `user_repo`, `user_project_active`, `not_user_project:PHD`). Hoy esos hechos viven solo en `messages` y se borran con la compaction. Fix en `bot.py` post-handler de “corrección”: detectar la corrección + `memory.set_profile_fact(...)`.
2. **Subir `max_messages` a 200 y NO limpiar provider_session en compaction si provider=anthropic.** Anthropic Claude tiene prompt caching prefijal — la sesión puede vivir mucho más sin re-cobrar tokens, y mantienes la conversación real.
3. **Desactivar el handler de identidad para mensajes genéricos.** Triggerear solo cuando el regex matchee explícitamente `\b(qui[eé]n eres|qu[eé] (modelo|bot|ai|llm) eres|sos claude|are you claude)\b`. Quitar el match por palabras como “Opción” o “Ok”.

Con esos 3 fixes, el bot deja de re-introducirse como “Soy Dr. Strange en el daemon local…” ante mensajes inocuos, deja de olvidar que el repo es `Hecpac/hector-services-site`, y deja de perder el hilo cuando una conversación pasa de 80 turnos o las 2 horas.

---

## 13. Anexo — métricas vivas del último turno

```
session_id          : tg-574707975
provider_session_id : 2bb76a58-f264-4f47-9d37-781d828ba774
mode                : ops (acabó como ops, vino de browse, antes coding)
autonomy_mode       : assisted
current_goal        : "No haz abierto ninguna página aún"   ← ruido, era queja
rolling_summary     : 11.996 / 12.000 chars (a punto de truncar el inicio)
last_turn_summary   : 240 chars
step_budget         : 2
steps_taken         : 1
verification_status : unknown
prompt_chars        : 4.028
system_prompt_chars : 67.958
evidence_pack_chars : 267
estimated_total_input_tokens : 17.996
```

Pending approvals colgadas: 8 (`0e8fd603…`, `4885d761…`, `5442950…`, `6be1a960…`, `721f2d22…`, `85538fa2…`, `90498b39…`, `c6ae52ec…`).
