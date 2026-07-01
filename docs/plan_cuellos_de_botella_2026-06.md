# Plan de remediación — cuellos de botella del agente (2026-06-11)

Diagnóstico de cinco áreas reportadas por Héctor: automatización de tareas,
manipulación de URLs, mantenimiento de tareas largas, ejecución de comandos,
y pérdida de identidad. Cada finding referencia `archivo:línea` verificado en
el código a fecha de este documento. El plan de remediación está al final,
ordenado por impacto/esfuerzo, con criterio de verificación por paso.

**Patrón transversal**: el sistema falla en silencio en los bordes —
truncamientos sin marcador, timeouts tragados, aprobaciones que expiran sin
aviso, recovery que descarta trabajo parcial, reintentos sin tope. La capa de
seguridad (triple-AND, sandbox, breakers) es sólida y deliberada; lo que falta
es que cada límite *avise* cuando recorta y que el progreso sea reanudable.

---

## Findings

### F1 — Automatización de tareas

| # | Finding | Ubicación | Tipo |
|---|---------|-----------|------|
| F1.1 | Loop infinito de "verification pending": `_defer_autonomous_job` re-encola con `retry_delay_seconds=0` y **sin límite de reintentos**; tarea que nunca pasa a passed/failed se recicla indefinidamente | `task_handler.py:832-842`, `task_handler.py:2327-2344` | **Bug** |
| F1.2 | Sin timeout wall-clock total por tarea: solo timeouts por fase (worker 120s, research 90s, verify 60s) + `max_iterations=3`. `max_wallclock_s`/`max_cost_usd` existen en `agent_loop.py:92-96` pero nunca se aplican | `agent_loop.py:87-96`, `coordinator.py:80-82`, `INTERNAL_WIRING.md` §7 | TODO abierto (wave 2) |
| F1.3 | Drenaje lento: el background runner ejecuta 1 handler por ciclo; N jobs encolados ⇒ cada uno espera ≥ `runner.interval` | `daemon.py:351-369` | Fragilidad |
| F1.4 | Idempotencia de `resume_key` traga objetivos nuevos si el job previo está en `retrying` | `jobs.py:108` | Fragilidad |
| F1.5 | Reconciliación de huérfanos limitada a 100 jobs; jobs `coordinator.autonomous_task` en `running` no se reclaman automáticamente (solo los `pending_verification_reconciliation` tras 120s) | `daemon.py:143-168`, `daemon.py:407-432` | Fragilidad |
| F1.6 | Concurrencia: máx. 4 workers autónomos (`max_autonomous_workers`); el 5º queda en cola de capacidad | `task_handler.py:71`, `task_handler.py:1178-1196` | Diseño |

### F2 — Manipulación de URLs / navegación

| # | Finding | Ubicación | Tipo |
|---|---------|-----------|------|
| F2.1 | Contenido de página truncado a 8.000 chars **sin marcador de truncamiento**; excepciones de extracción se tragan (`return ""`) | `browser.py:412`, `browser.py:495-503` | **Fragilidad alta** |
| F2.2 | Timeouts hardcodeados (10s JS-heavy + 1.5s extra, 8s resto); al expirar se hace `pass` silencioso ⇒ contenido incompleto sin aviso | `browser.py:483-492` | Fragilidad |
| F2.3 | Detección de login wall solo con señales en inglés ("sign in", …); falla en contenido en español | `bot_helpers.py:2546-2561` | Fragilidad |
| F2.4 | `_normalize_url` / `_strip_url_punctuation` puede mutilar query strings; sin validación de host/puerto | `bot_helpers.py:2856-2865` | Fragilidad |
| F2.5 | Solo 2 reintentos CDP con 1s de espera; insuficiente si Chrome está arrancando | `browser.py:406`, `browser.py:423-438` | Fragilidad |
| F2.6 | Selección de página CDP por substring (`pattern in page.url`); si no matchea crea página nueva | `browser.py:506-517` | Fragilidad |
| F2.7 | Tres backends (Jina / CDP-Playwright / fxtwitter) elegidos por estrategia de dominio; el fallback en cadena enmascara la causa raíz del fallo original | `browse_handler.py:58-223`, `bot_helpers.py:2498-2543` | Diseño con fricción |

### F3 — Tareas largas / persistencia

| # | Finding | Ubicación | Tipo |
|---|---------|-----------|------|
| F3.1 | El recovery tras reinicio del daemon **reinicia `coordinator.run()` completo**: carga artefactos de research/synthesis del scratch pero la fase a medias arranca de cero | `task_handler.py:1264-1275`, `task_handler.py:1436-1459` | **Fragilidad alta** |
| F3.2 | Truncamiento lossy entre fases: synthesis→impl e impl→verify pasan máx. 48.000 chars; cada resultado de worker se resume a 16.000 | `coordinator.py:18-26`, `coordinator.py:207`, `coordinator.py:276-285` | Fragilidad |
| F3.3 | Si la destilación LLM falla (timeout, circuito abierto) cae a recorte mecánico head+tail (`degraded_compaction=True`); los workers pierden visibilidad del trabajo previo | `coordinator.py:629-726` | Fragilidad |
| F3.4 | Circuit breakers congelan trabajo en curso: >$10/h rolling o >1M tokens/5h; el freeze manual nunca se auto-limpia (requiere `/unfreeze`) | `observation_window.py:233-244`, `observation_window.py:355-371` | Diseño |
| F3.5 | El muro de 300s del turno de chat del brain fuerza delegación de todo trabajo pesado | `brain.py:530,631,726`, `config.py:91` | Diseño deliberado |

### F4 — Ejecución de comandos

| # | Finding | Ubicación | Tipo |
|---|---------|-----------|------|
| F4.1 | Output de subproceso truncado a 20.000 chars por defecto (se queda con la cola); logs largos (pytest, npm) pierden el medio | `subprocess_runner.py:118` | Fricción |
| F4.2 | Aprobaciones Tier 3 expiran en silencio a los 900s (15 min); sin reintento ni notificación de expiración | `approval.py:22`, `approval.py:170-174` | Fricción |
| F4.3 | Sandbox bloquea operadores de shell (`;` `\|` `&` `$()`) con mensajes vagos; obliga a envolver en `bash -c` (que se desenvuelve y re-escanea) | `sandbox.py:251-252` | Diseño con fricción |
| F4.4 | Allowlist de binarios por perfil (surgical/engineer/admin); binario fuera de perfil ⇒ `PermissionError` | `sandbox.py:11-78` | Diseño |
| F4.5 | Backstop PreToolUse deniega Bash brain-lane con patrones Chrome/CDP/Playwright/puertos 9222-9250 — correcto como diseño, pero también bloquea debugging legítimo desde chat | `adapters/anthropic.py:83-104`, `adapters/anthropic.py:541-591` | Diseño deliberado |
| F4.6 | Kill escalation agresiva: SIGTERM + 2s ⇒ SIGKILL; procesos con cleanup lento mueren a la fuerza | `subprocess_runner.py:191-193` | Fricción |

### F5 — Pérdida de identidad (causa raíz identificada)

| # | Finding | Ubicación | Tipo |
|---|---------|-----------|------|
| F5.1 | **`build_context()` no inyecta SOUL.md / IDENTITY.md / BOOT_PROTOCOL.md.** Tras compactación + reset de provider session, el contexto reconstruido contiene session_state, facts, rolling_summary y últimos 20 mensajes — y **ninguna capa de identidad** | `memory.py:1306-1426`, `brain.py:933-939` | **Bug (causa raíz)** |
| F5.2 | Ruta crítica completa: >200 mensajes ⇒ `compact_session_messages()` borra mensajes viejos ⇒ reset de provider session (`provider_session_id = None`) ⇒ nueva sesión construida solo con `build_context()` | `memory.py:754-800`, `brain.py:464-473` | Bug (consecuencia de F5.1) |
| F5.3 | `rolling_summary` se trunca a 20.000 chars descartando lo más antiguo | `memory.py:309` | Fragilidad |
| F5.4 | `startup_context()` trunca silenciosamente archivos de identidad si exceden presupuesto de chars; ni el arranque garantiza identidad completa | `workspace.py:361-376`, `workspace.py:573-576` | Fragilidad |
| F5.5 | `IDENTITY_ANCHOR` (~200 chars, última línea del system prompt) es el único refuerzo de recencia; no sobrevive si el prompt se recorta antes | `brain.py:314-319` | Fragilidad |
| F5.6 | Sub-agentes (Alma, Hex, Echo…) reciben solo su propio SOUL.md, sin herencia del padre — aislamiento deliberado, pero "Dr. Strange" no existe en los workers | `agents.py:1879-1893` | Diseño |
| F5.7 | SOUL.md que no parsea ⇒ fallback silencioso de modelo a sonnet-4-6 (salvo `CLAW_STRICT_SOUL_MODEL=1`) | `agents.py:32-35` | Fragilidad |
| F5.8 | La dimensión `state_amnesia` cubre contradicciones de estado del sistema, pero no existe chequeo equivalente para deriva de identidad | `verification/dimensions/state_amnesia.md` | Gap |

---

## Plan de remediación

Cada paso con su verificación (contrato goal-driven). Las olas son
independientes: se puede cortar después de cualquiera y dejar el sistema
mejor que antes.

**El orden de ejecución vigente es el "Orden integrado" al final del
documento**, que entrelaza estas olas con los fixes de deuda estructural
(Anexo A) bajo la regla: *cuellos de botella primero; la deuda se paga solo
cuando está en el camino crítico de un fix de cuello, o cuando es casi
gratis.*

### Ola 1 — Identidad persistente (F5.1, F5.2) — barato, máximo impacto visible

1. Extraer un bloque de identidad mínimo (SOUL.md condensado + IDENTITY_ANCHOR
   ampliado) cargado una vez y cacheado en `MemoryService`.
   → verify: test unitario que el bloque carga y no excede presupuesto fijo.
2. Inyectar ese bloque incondicionalmente al inicio de `build_context()`
   (`memory.py:1306`), incluido el camino `include_history=False` post-compactación.
   → verify: test que simula compactación + provider reset y asserts de que el
   contexto resultante contiene el ancla de identidad.
3. Emitir evento `observe_stream` (`identity_block_injected` /
   `identity_block_truncated`) cuando el bloque se inyecta recortado.
   → verify: test de evento emitido; `think tail --type identity_block_truncated`.

### Ola 2 — Tope al loop de reintentos (F1.1) — elimina trabajo zombi

4. Añadir contador de deferrals al checkpoint del job y límite
   (`max_verification_deferrals`, p.ej. 5) en `_defer_autonomous_job`
   (`task_handler.py:2327`); al exceder ⇒ terminal `failed` con razón
   `verification_stalled` + notificación.
   → verify: test que una tarea perpetuamente "pending" termina `failed` en ≤N
   reintentos; ningún job queda en `retrying` perpetuo.
5. Backoff exponencial en lugar de `retry_delay_seconds=0`.
   → verify: test de progresión de `next_run_at`.

### Ola 3 — Truncamientos honestos (F2.1, F3.2, F3.3, F4.1) — el brain debe saber qué le falta

6. Marcador estándar de truncamiento (`[truncated: kept X of Y chars]`) en:
   extracción de página (`browser.py:495-503`), resúmenes inter-fase
   (`coordinator.py:207,280-285`), output de subproceso ya lo tiene — unificar
   formato.
   → verify: tests por sitio de truncamiento; grep de formato unificado.
7. Propagar `degraded_compaction=True` como advertencia visible en el prompt de
   la fase siguiente, no solo como flag interno.
   → verify: test de que el prompt de impl/verify contiene la advertencia
   cuando la destilación degradó.
8. Hacer `max_output_chars` de subprocess configurable por contexto
   (brain vs worker), manteniendo 20k como floor de brain.
   → verify: test de override en worker lane.

### Ola 4 — Presupuesto global de tarea (F1.2) — cierra el TODO de wave 2

9. Aplicar `max_wallclock_s` y `max_cost_usd` ya declarados en
   `agent_loop.py:92-96`: chequeo al inicio de cada iteración; al exceder ⇒
   `status="exhausted"` con razón explícita.
   → verify: test con reloj/coste simulado; actualizar
   `test_architecture_invariants.py` si se promueve a invariante;
   actualizar `INTERNAL_WIRING.md` §7 (cerrar TODO) + `describes_commit`.

### Ola 5 — Resume granular por fase (F3.1) — el más caro, habilita tareas largas reales

10. Persistir `last_completed_phase` en el checkpoint de orquestación y hacer
    que `coordinator.run()` acepte `start_phase`, cargando artefactos de
    fases completadas desde scratch en lugar de re-ejecutarlas.
    → verify: test de kill+resume entre fases que asserts: research y synthesis
    NO se re-ejecutan, impl arranca de su inicio.
11. (Extensión) checkpoint intra-fase para implementation: persistir resultados
    por worker-task completado y saltar los ya hechos en resume.
    → verify: test con 3 sub-tareas, kill tras la 2ª, resume ejecuta solo la 3ª.

### Ola 6 — Pulido de URLs y aprobaciones (F2.2-F2.5, F4.2) — fricción menor

12. Señales de login wall en español además de inglés (`bot_helpers.py:2546`).
13. Reintentos CDP 2→4 con backoff (`browser.py:406`).
14. Notificación Telegram al expirar una aprobación (no solo expiración
    silenciosa) con opción de re-crear la solicitud (`approval.py:170`).
15. Timeouts de navegación configurables vía `AppConfig` en lugar de
    hardcodeados (`browser.py:483-492`).
    → verify (12-15): test unitario por ítem.

### Fuera de alcance deliberado

- F5.6 (aislamiento de sub-agentes), F3.5 (muro 300s del brain), F4.3-F4.5
  (sandbox y backstop PreToolUse), F3.4 (breakers): son diseño intencional
  documentado en `INTERNAL_WIRING.md`; no tocar sin decisión explícita.

### Nota de proceso

Olas 1-2 tocan símbolos descritos en `INTERNAL_WIRING.md` (TaskHandler,
MemoryService/brain context): actualizar `describes_commit` y `last_verified`
en el mismo commit. Cada ola pasa `tests/test_architecture_invariants.py`,
`uvx ruff check claw_v2 tests` y la suite afectada antes de promover.

---

## Anexo A — Deuda de estructura: `adapters/` y `verification/`

Análisis profundo posterior al diagnóstico de cuellos (2026-06-11). Deuda =
lo que no frena el runtime hoy pero encarece cada cambio futuro. Naturaleza
distinta por carpeta: en `adapters/` es **concentración** (todo en un archivo,
intesteable); en `verification/` es **rigidez** (extender exige código,
versionado decorativo, andamiaje a medio cablear).

### A.1 Findings de deuda — `adapters/`

| # | Deuda | Ubicación | Nota |
|---|-------|-----------|------|
| DA.1 | `ClaudeSDKExecutor` monolito de 940 líneas con 6 responsabilidades: `_build_options()` mezcla auth+tools+persona+agents+hooks+thinking+MCP; `_build_hooks()` son 193 líneas con los 5 hooks (incl. backstop CDP y approval gates) entrelazados con telemetría | `adapters/anthropic.py:386-459`, `:516-709` | Deuda madre; causa DA.2 |
| DA.2 | El adapter principal tiene **0 tests unitarios propios** (solo mocks vía router); Codex (secundario) tiene 243 líneas de tests; Ollama 0 | `tests/` (ausencia de `test_anthropic_adapter*`, `test_ollama*`) | La seguridad más delicada del sistema sin red |
| DA.3 | Persona de Dr. Strange + silence directive hardcodeadas en el executor | `adapters/anthropic.py:410-416` | Conecta con F5 (identidad): debería haber un único origen del bloque de identidad |
| DA.4 | Resolución de API key escanea dotfiles del shell (`~/.zshrc`, `~/.profile`) | `adapters/anthropic.py:919-940` | Expansión de scope; debería ser env-only |
| DA.5 | Enforcement de tools divergente por proveedor: Anthropic = hooks PreToolUse + approval gates; OpenAI = callback sin enforcement equivalente; Ollama = schemas built-in. La ABC estandariza `complete()` pero no cómo se ejecutan tools | `adapters/openai.py:157-209`, `adapters/ollama.py:177-304` | El triple-AND tiene tres implementaciones distintas |
| DA.6 | Conocimiento de sesiones del proveedor filtrado al router: solo reconoce `resp_*` de OpenAI | `llm.py:497-510` | Sesión se pierde en silencio en otros fallbacks |
| DA.7 | `google.py` semi-huérfano (stub advisory-only, 1 test) + `models/code-auditor.Modelfile` sin decisión de mantener/podar | `adapters/google.py:1-75` | Lo caro es el limbo, no la decisión |

### A.2 Findings de deuda — `verification/`

| # | Deuda | Ubicación | Nota |
|---|-------|-----------|------|
| DV.1 | Tres enums cerrados que exigen tocar código para extender: `external_check.kind` (5 tipos), `probe_kind` (5), sub-contratos Bash (solo `git_commit`) | `success_contract.py:34-49`, `:103-115`, `local_tool_contracts.py:113-141` | Contradice el patrón "política como datos" del resto del sistema |
| DV.2 | `schema_version` declarado en cada artifact pero el gate nunca lo compara; artifacts viejos pasarían silenciosamente tras un cambio de schema. Ídem: renombrar una dimensión del judge rompe re-scoring histórico sin aviso | `success_contract.py:26`, `promote_gate.py:170-178` | También es gap de seguridad (cuello) |
| DV.3 | `attach_artifact_to_result` consulta solo contratos LOCALES; los EXTERNAL viven en otro registry que este camino ignora — costura mal puesta que F3b.1 heredará | `local_tool_runner.py:140-147` | Pagar justo antes de cablear fetchers |
| DV.4 | Garantías por convención, no por construcción: aislamiento del judge sin verificación runtime; sanitización de `task_id` por parcheo; catch-all del gate que enmascara bugs propios como bloqueos | `runner.py:85-129`, `transcript.py:139-140`, `promote_gate.py:336` | Fail-closed correcto pero opaco |
| DV.5 | Tombstones sin barrer: `claw_v2/verification.py` (sombra, nadie lo importa) y `verification_profiles.py` (legacy coexistiendo sin integrarse) | raíz de `claw_v2/` | Confunde al lector, no al runtime |

### A.3 Fixes de deuda

**D1 — Partir `anthropic.py` (940 → 4 módulos)** [resuelve DA.1]
`anthropic.py` (~250: adapter + flujo `_run`), `anthropic_hooks.py` (~220:
los 5 hooks como funciones nombradas con dependencias explícitas),
`anthropic_options.py` (~180: options builder, agents, MCP de delegación),
`anthropic_auth.py` (~40). Movimiento puro de código, sin cambiar
comportamiento.
→ verify: suite completa pasa sin cambios; actualizar `INTERNAL_WIRING.md`
(`describes_commit`) en el mismo commit.

**D2 — Tests de hooks** [resuelve DA.2; prerequisito: D1]
`tests/test_anthropic_hooks.py`: backstop CDP niega cada patrón en brain-lane
y permite en worker; `ApprovalPending`/`PermissionError` → deny con
systemMessage; PostToolUseFailure registra mutación aunque el tool falle;
`record_tools_executed` en todo camino de excepción.
→ verify: cobertura de los 5 hooks + test de regresión del invariante CDP.

**D3 — Persona fuera del executor** [resuelve DA.3; sinergia con Ola 1]
Bloque de identidad + silence directive a builder único (workspace/config),
inyectado. Un solo origen para startup, `build_context()` y adapter.
→ verify: golden test — system prompt idéntico byte a byte antes/después.

**D4 — Auth env-only** [resuelve DA.4]
Eliminar escaneo de dotfiles; key de env o `~/.claw/env`; si falta ⇒
`AdapterUnavailableError` con mensaje accionable.
→ verify: test de que un dotfile fixture con key NO se resuelve.

**D5 — Contrato de sesión por adapter** [resuelve DA.6]
`ProviderAdapter.owns_session_id(session_id) -> bool` (default False);
OpenAI overridea con prefijo `resp_`; el router pregunta, no conoce formatos.
→ verify: test de fallback con descarte vs. conservación de sesión.

**D6 — Ollama timeout + tests; Google: decidir** [resuelve DA.7 + cuello]
Envolver `client.chat()` con `timeout_s`; Google se documenta como
advisory-only en `INTERNAL_WIRING.md` o se poda junto con el Modelfile.
→ verify: test de timeout; entrada en wiring o commit de borrado.

**D7 — (proyecto aparte) Contrato de enforcement de tools** [mitiga DA.5]
Corto plazo: `enforces_tool_policy: bool` en la ABC y el router rechaza lanes
tool-capable en adapters sin enforcement — asimetría silenciosa ⇒ error
explícito. La unificación real (OpenAI vía runtime_policy) queda fuera de
este plan.
→ verify: rutear worker lane a adapter sin enforcement falla en validación.

**D8 — Validar `schema_version`** [resuelve DV.2; ~10 líneas]
En `_deserialize_success_condition`: mismatch ⇒ `pending_verification` con
razón `schema_version_mismatch` (no `failed`).
→ verify: `test_gate_artifact_schema_version_mismatch()`.

**D9 — Timeout + cap de input en regex del gate** [cuello + deuda]
Cap de ~10K chars al texto evaluado y guard del patrón en
`success_contract.py:175-181`; fix local al evaluador, mantiene pureza.
→ verify: patrón catastrófico retorna `regex_invalid` en <1s.

**D10 — Registry unificado de contratos** [resuelve DV.3; prerequisito de F3b.1]
`get_tool_contract(tool_name)` consultando LOCAL+EXTERNAL; runner y gate
consumen solo esa función.
→ verify: tool externo recibe artifact por el mismo camino que Write/Edit.

**D11 — Kinds pluggables** [resuelve DV.1; hacer junto a F3b.1]
Registries de `external_check.kind` y `probe_kind`; sub-contratos Bash como
datos. Objetivo mínimo: añadir el próximo kind = registrar una función.
→ verify: kind ficticio registrado valida end-to-end.

**D12 — Tombstones + eventos enriquecidos** [resuelve DV.5 + parte de DV.4]
Borrar `claw_v2/verification.py` (confirmar 0 imports); marcar
`verification_profiles.py` legacy con puntero; añadir `tier`, `tool_name`,
`error_count`, `reason` al payload de eventos del gate
(`promote_gate.py:376-385`).
→ verify: grep sin imports; test de payload.

---

## Orden integrado (vigente)

Regla: cuellos de botella primero; la deuda se paga solo cuando está en el
camino crítico de un fix de cuello, o cuando es casi gratis. Evitar la
trampa inversa ("primero limpiar, luego arreglar"): la limpieza sin presión
de un fix concreto se sobre-diseña mientras el loop infinito sigue girando.

| Paso | Contenido | Justificación |
|------|-----------|---------------|
| 1 | **Ola 1** (identidad en `build_context()`) + **Ola 2** (tope de deferrals) | Cuellos puros en zona sin deuda; máximo impacto visible |
| 2 | **D8 + D9** | Deuda casi gratis que es a la vez gap de seguridad |
| 3 | **Ola 3** (truncamientos honestos) | Cuello puro |
| 4 | **D1 → D2** (split `anthropic.py` + tests de hooks) | Prerequisito obligatorio del paso 5: tocar el executor sin tests propios es inaceptable |
| 5 | Retry de transitorios en Anthropic (cierra el callejón anti-replay) + timeout Ollama (D6) | Cuellos de `adapters/`, ya con red de tests |
| 6 | **D10 → fetchers F3b.1 → D11** | La deuda de verification se paga en la víspera de su cuello (tareas Tier 3 que nunca cierran), no antes — los fetchers reales aún pueden cambiar la interfaz |
| 7 | **Ola 4** (presupuesto global) + **Ola 5** (resume granular) | Estructurales de tareas largas |
| 8 | D3, D4, D5, D12, **Ola 6**, D7 | Limpieza, pulido y el proyecto grande, según apetito |

Dependencias duras: D2 requiere D1; el paso 5 requiere el 4; F3b.1 requiere
D10. Todo lo demás es paralelizable.
