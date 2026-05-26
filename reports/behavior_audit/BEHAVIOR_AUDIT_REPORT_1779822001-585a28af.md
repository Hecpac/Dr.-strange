---
run_id: 1779822001-585a28af
generated_by: reports/behavior_audit/extract_behavior_audit.py
source: /Users/hector/Projects/Dr.-strange/reports/behavior_audit/extract_behavior_audit.py
started_at: 2026-05-26T19:00:01.671187+00:00
completed_at: 2026-05-26T19:00:02.198859+00:00
canonical: false
input_db: /Users/hector/Projects/Dr.-strange/data/claw.db
sample_size: 678
---

# Behavioral Audit Report — Dr. Strange

Generated from read-only SQLite and approval-store extraction. JSONL sample:
`reports/behavior_audit/behavior_cases_sample_1779822001-585a28af.jsonl`.

## 1. Resumen Ejecutivo

- Evidencia real: `messages`, `observe_stream`, `agent_tasks`, `agent_jobs`, `facts`, `task_outcomes`, `session_state` y archivos JSON de approvals.
- Casos sanitizados generados: **678**.
- El patrón dominante de riesgo operativo es `completed_unverified` / `needs_verification`: **202** casos etiquetados.
- Tool use observado: **840** eventos/casos con tools; principales tools abajo.
- Permisos: **36** casos pidieron aprobación; **284** aparecen como posibles permisos faltantes por heurística.
- Costo estimado agregado en la muestra: **$3.415058**. Nota: OpenAI/Codex reportan costo `0.0` en adapters, por lo que esta métrica subestima costo API real.
- Latencia Telegram mediana: **38878.0 ms**; p95: **237570.8 ms** cuando había eventos `telegram_latency`.
- Inferencia: Hector usa el agente principalmente como operador conversacional con ejecución local, revisión, continuación contextual y automatización ligera.
- Pregunta abierta: qué porcentaje de `completed_unverified` representa trabajo útil no cerrado versus falsos positivos que deben reconciliarse.

## 2. Métricas Principales

| Métrica | Valor |
|---|---:|
| Casos totales | 678 |
| Approvals solicitados | 36 |
| Posibles approvals innecesarios | 30 |
| Posibles approvals faltantes | 284 |
| Completions no verificadas | 202 |
| Posibles false success | 0 |
| Costo estimado total | $3.415058 |
| Mediana latencia ms | 38878.0 |
| P95 latencia ms | 237570.8 |

### Distribución Por Etiqueta

| Item | Casos |
|---|---:|
| `intent_correct` | 603 |
| `missing_permission` | 284 |
| `unverified_completion` | 202 |
| `good_tool_use` | 192 |
| `memory_good` | 177 |
| `intent_missed` | 75 |
| `wrong_tool` | 58 |
| `good_workflow_candidate` | 35 |
| `unnecessary_permission` | 30 |
| `underexecuted` | 11 |
| `memory_bad` | 11 |
| `missed_workflow_candidate` | 7 |
| `overcomplicated` | 7 |


### Tipos De Caso

| Item | Casos |
|---|---:|
| `task_ledger:telegram:brain_fallback:completed_unverified` | 201 |
| `tool_call` | 200 |
| `message_turn` | 85 |
| `memory_write:task_outcome:telegram_message` | 78 |
| `error_or_fallback_event` | 50 |
| `approval:promote_perf-optimizer` | 17 |
| `memory_write:fact` | 16 |
| `approval:browser_use_task` | 8 |
| `memory_write:task_outcome:browse` | 6 |
| `approval:codex_computer_task` | 5 |
| `approval:tool:GPTImage` | 4 |
| `task_ledger:evidence_gate:browse:failed` | 3 |
| `approval:promote_self-improve` | 1 |
| `task_ledger:web:brain_fallback:completed_unverified` | 1 |
| `task_ledger:evidence_gate:publish:failed` | 1 |
| `task_ledger:telegram_imperative:ops:succeeded` | 1 |


### Riesgo Y Autonomía

| Item | Casos |
|---|---:|
| `high` | 313 |
| `low` | 210 |
| `medium` | 155 |


| Item | Casos |
|---|---:|
| `durable_task` | 208 |
| `assisted` | 206 |
| `approval_gated` | 119 |
| `learning_loop` | 84 |
| `human_approval_gate` | 35 |
| `memory` | 16 |
| `autoexecuted_policy_bypass` | 10 |


## 3. Top 10 Patrones De Éxito

Evidencia: casos con `intent_correct` y/o `good_tool_use`.

- `tool_43214` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Edit'], verification=`unknown`. Request: Tool event: Edit; Outcome: sdk_post_tool_use
- `tool_43215` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Edit'], verification=`unknown`. Request: Tool event: Edit; Outcome: sdk_post_tool_use
- `tool_43217` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Edit'], verification=`unknown`. Request: Tool event: Edit; Outcome: sdk_post_tool_use
- `tool_43218` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Bash'], verification=`unknown`. Request: Tool event: Bash; Outcome: sdk_post_tool_use
- `tool_43266` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Grep'], verification=`unknown`. Request: Tool event: Grep; Outcome: sdk_post_tool_use
- `tool_43267` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Grep'], verification=`unknown`. Request: Tool event: Grep; Outcome: sdk_post_tool_use
- `tool_43268` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Read'], verification=`unknown`. Request: Tool event: Read; Outcome: sdk_post_tool_use
- `tool_43272` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Edit'], verification=`unknown`. Request: Tool event: Edit; Outcome: sdk_post_tool_use
- `tool_43273` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Edit'], verification=`unknown`. Request: Tool event: Edit; Outcome: sdk_post_tool_use
- `tool_43274` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Edit'], verification=`unknown`. Request: Tool event: Edit; Outcome: sdk_post_tool_use


Patrones observados:
- Brain-first y `semantic_turn_trace` dan buena señal para continuaciones.
- `sdk_post_tool_use` deja rastro suficiente para reconstruir tool use.
- `telegram_latency` permite medir experiencia real, no solo inferirla.
- `NaturalLanguageRenderer` y sanitizers reducen exposición de labels internos en varias rutas.
- `task_ledger_created` + `task_ledger_terminal` dan cierre auditable.
- KAIROS emite acciones y supresiones, útil para distinguir ruido de alertas.
- `critical_action_verification` captura recomendación, riesgo y necesidad de aprobación.
- El ledger conserva suficientes campos para detectar `completed_unverified`.
- Learning outcomes (`task_outcomes`) registran lecciones útiles.
- El full suite previo pasó, lo que respalda que estas rutas tienen cobertura.

## 4. Top 10 Patrones De Fallo

- `task_ab934416585b` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Grep', 'Read'], verification=`needs_verification`. Request: Revisa que todos Los cambios quedaron aplicados; Outcome: brain tool-use turn: 11 tool calls (unverified)
- `task_fa8276466ba9` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Edit', 'Write'], verification=`needs_verification`. Request: Continúa; Outcome: brain tool-use turn: 3 tool calls (unverified)
- `task_eeea0a3c31c7` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Grep', 'Write'], verification=`needs_verification`. Request: Procede con esto; Outcome: brain tool-use turn: 12 tool calls (unverified)
- `task_5d21fcfe9dfa` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Edit', 'Write'], verification=`needs_verification`. Request: F3a-extension está parcialmente aprobado, pero no lo marques done todavía. Ejecuta F3a-ext.1 para cerrar bypasses semánt...[truncated]; Outcome: brain tool-use completed with warnings: 1 substep failure(s)
- `task_6368b63f33e5` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Edit', 'Grep', 'Read', 'Write'], verification=`needs_verification`. Request: OK final: marca F3a como done. No ejecutes F3b todavía. Siguiente fase: F3a-extension para cerrar los Tier 2 locales res...[truncated]; Outcome: brain tool-use turn: 14 tool calls (unverified)
- `task_4ed78375e69f` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Edit'], verification=`needs_verification`. Request: F3a.1 está aprobado como runtime artifact generation, pero no marques F3a como done todavía. Ejecuta F3a.2 para cerrar e...[truncated]; Outcome: brain tool-use completed with warnings: 1 substep failure(s)
- `task_a115308fb3e4` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Edit', 'Grep', 'Read', 'Write'], verification=`needs_verification`. Request: F3a está parcialmente aprobado, pero no lo marques done todavía. Hay un posible bypass: el promote gate solo actúa si ex...[truncated]; Outcome: brain tool-use completed with warnings: 2 substep failure(s)
- `task_7e69ca7cab63` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Edit', 'Grep', 'Read', 'Write'], verification=`needs_verification`. Request: OK final: marca F2.5 + F2.5.1 como done. No ejecutes F3 externa todavía. No tocar X, LinkedIn, HeyGen, deploy ni GitHub ...[truncated]; Outcome: brain tool-use completed with warnings: 1 substep failure(s)
- `task_2a285ab62652` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Edit', 'Read'], verification=`needs_verification`. Request: F2.5 está casi aprobado, pero no lo marques done todavía. Hay un bypass crítico: en task_handler.py, si gate_terminal_st...[truncated]; Outcome: brain tool-use turn: 6 tool calls (unverified)
- `task_67387faf8807` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Edit', 'Grep', 'Read', 'Write'], verification=`needs_verification`. Request: OK final para marcar F1+F2 como done. No ejecutes F3 todavía. Antes de pasar a tools reales, quiero una F2.5: 1. Integra...[truncated]; Outcome: brain tool-use turn: 8 tool calls (unverified)


Patrones observados:
- Muchas tareas quedan como `completed_unverified` con `needs_verification`.
- Algunos eventos de tool failure aparecen sin una remediación estructurada visible en el caso.
- El costo real queda incompleto cuando el adapter reporta `0.0`.
- Las rutas de approvals mezclan approvals humanos, internos y KAIROS; conviene separar semánticamente.
- El outcome puede ser una respuesta conversacional sin evidencia ejecutable.
- La muestra tiene pocos `facts`, pero varios `task_outcomes`; memoria aprende más de resultados que de hechos explícitos.
- Los casos de external content requieren etiqueta sistemática de `prompt_injection_risk`.
- Varias decisiones dependen de heurística de texto y podrían fallar ante frases nuevas.
- La ausencia de `agent_jobs` activos sugiere que trabajos background no están usando ese servicio o no hay cola viva.
- La auditoría necesita más correlación formal entre user turn, trace_id, task_id y approval_id.

## 5. Fricción De Permisos

| Señal | Evidencia |
|---|---|
| Approvals en muestra | 36 casos |
| Posibles innecesarios | 30 por heurística low-risk/read-only |
| Posibles faltantes | 284 por high-risk tool sin approval visible |
| Eventos críticos | `critical_action_verification`, `critical_action_execution`, approval JSON store |

Recomendación: separar `approval_requested_by_policy`, `approval_requested_by_verifier`, `approval_requested_by_kairos` y `approval_user_visible` como campos distintos.

## 6. Calidad De Uso De Tools

### Tools Más Frecuentes

| Item | Casos |
|---|---:|
| `Bash` | 284 |
| `Write` | 174 |
| `Read` | 142 |
| `Edit` | 114 |
| `Grep` | 84 |
| `Glob` | 38 |
| `GPTImage` | 4 |


Evidencia: `sdk_post_tool_use`, `sdk_post_tool_use_failure`, `action_proposed`, `action_executed`, `AUTONOMY_BYPASS`.

Inferencia: el uso de tools es trazable, pero la calidad depende de correlación; ahora se reconstruye por ventanas temporales, no por un `turn_id` explícito.

## 7. Calidad De Verificación

Evidencia principal: `agent_tasks.status`, `agent_tasks.verification_status`, `brain_tooluse_ledger_needs_verification`, `cycle_verification_complete`, `critical_action_verification`.

Hallazgo: **202** casos quedan con etiqueta `unverified_completion`. Esto no significa necesariamente fallo, pero sí deuda operacional.

Recomendación: ninguna tarea debería terminar en estado visible de éxito si `verification_status` no está en `passed/verified/ok` o si no hay evidence manifest.

## 8. Calidad De Memoria

Evidencia:
- `facts`: memoria factual explícita.
- `task_outcomes`: aprendizaje operacional.
- `messages`: memoria conversacional.
- `session_state`: estado activo.

Hallazgo: hay más aprendizaje por outcomes que por facts. Esto es bueno para mejorar comportamiento, pero puede perder preferencias/datos durables si no se promueven a facts confiables.

Recomendación: añadir eval que verifique que feedback repetido de Hector se convierte en fact durable solo cuando cumple criterios de confianza y redacción.

## 9. Oportunidades De Workflows

Casos candidatos detectados: **35**.

- `msg_497` — intent=`unknown`, task=`message_turn`, tools=[], verification=`unknown`. Request: Dale actualiza la memoria para arrancar phase 4d; Outcome: **🛑 Push back antes de tocar memoria.** Verifico la premisa porque "arrancar Phase 4d" tiene tres trigger conditions explícitas en `docs/rec...[truncated] — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.
- `msg_519` — intent=`unknown`, task=`message_turn`, tools=[], verification=`unknown`. Request: Dale outreach; Outcome: **✅ Outreach list pool service DFW entregada a Telegram (msg_id 11051, CSV adjunto).** ## Lo que está en el CSV **30 pool service contractor...[truncated] — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.
- `msg_545` — intent=`unknown`, task=`message_turn`, tools=[], verification=`unknown`. Request: Dale entregame el video listo; Outcome: **Esta vez es real. Render firing, con evidencia.** ``` PID: bjwiz1nrh (background) Log: /tmp/...bjwiz1nrh.output prompt: 3312 chars cargado...[truncated] — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.
- `msg_557` — intent=`unknown`, task=`message_turn`, tools=[], verification=`unknown`. Request: Dale crea el video; Outcome: **Render disparado REAL esta vez. Evidencia inmediata.** ``` session_id: 62fad9d9b6904b52b968ea4dcd5b3c8a video_id: 61ed50fdc6b4460dbb519e21...[truncated] — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.
- `msg_577` — intent=`continue_active_mission`, task=`message_turn`, tools=[], verification=`unknown`. Request: Dale; Outcome: ¿Qué acción concreta quieres que ejecute? — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.
- `msg_601` — intent=`continue_active_mission`, task=`message_turn`, tools=[], verification=`unknown`. Request: Dale; Outcome: ¿Qué acción concreta quieres que ejecute? — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.
- `msg_643` — intent=`unknown`, task=`message_turn`, tools=[], verification=`unknown`. Request: Procede con esto; Outcome: ## Reconciliación F3b.0 ejecutada con evidencia real reproducible. **Marca F3b.0 como SUCCEEDED.** 161/161 tests passed, exit_code=0, log pe...[truncated] — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.
- `msg_653` — intent=`unknown`, task=`message_turn`, tools=[], verification=`unknown`. Request: [Imagen adjunta] path: [REDACTED:local_path] Continua; Outcome: ## F3b.1-reconcile ejecutado desde este path con evidencia reproducible. SUCCEEDED. ### 1. Estado de archivos **git status --short** (focuse...[truncated] — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.
- `msg_655` — intent=`new_task`, task=`message_turn`, tools=[], verification=`blocked`. Request: Abre F3b.1.5 para corregir el bug del daemon Telegram antes de F3b.2. Problema: Un dispatcher pre-brain está atrapando m...[truncated]; Outcome: Ahora mismo no tengo tareas corriendo ni en cola para esta sesion. Tambien tengo una accion pendiente de sesion: F3a-extension está parcialm...[truncated] — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.
- `msg_661` — intent=`unknown`, task=`message_turn`, tools=[], verification=`unknown`. Request: Continúa; Outcome: ## F3b.1.5.1 ejecutado. 214/214 tests passing en 0.29s. Falso positivo de "go" cerrado. ### Diff resumido **`claw_v2/bot.py`** — un solo arc...[truncated] — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.

Workflows sugeridos:
- Continuation resolver smoke: `Procede/Continúa/Dale` con state sources explícitos.
- Review/audit evidence-first: para “revisa/analiza/audita” con manifest de fuentes.
- NotebookLM workflow con límites y approval boundaries.
- External research workflow con untrusted context y prompt-injection checks.
- Tool-failure recovery workflow: retry/pivot/escalate con outcome explícito.

## 10. Recomendaciones Para Dr. Strange v3

1. Añadir `turn_id` global que conecte message, dispatch, tools, approvals, ledger, memory y response.
2. Convertir `completed_unverified` en estado temporal con SLA de reconciliación automática.
3. Hacer que approvals tengan `risk_basis`, `requested_by`, `visible_to_user`, `resolved_by`.
4. Agregar costo real por provider/model, especialmente OpenAI API.
5. Separar KAIROS proactive decisions de approvals humanos.
6. Promover workflows repetidos a playbooks con evals.
7. Añadir “behavior receipt” por turno con intent, tools, approval, evidence, verification.
8. Fortalecer prompt-injection labels para todo external-content tool output.
9. Crear dashboards por fricción: underexecuted, overcomplicated, missing_permission.
10. Reducir dependencia de correlación temporal; usar trace/root_trace_id como vínculo obligatorio.

## 11. Evals Nuevas Recomendadas

- `test_behavior_turn_receipt_links_message_tool_task_approval`.
- `test_completed_unverified_has_reconciliation_deadline`.
- `test_kairos_cannot_approve_foreign_pending_approval`.
- `test_openai_cost_estimate_nonzero_when_usage_present`.
- `test_external_content_tool_outputs_marked_untrusted`.
- `test_low_risk_readonly_does_not_request_approval`.
- `test_high_risk_bash_requires_policy_or_human_gate`.
- `test_memory_promotion_requires_trust_and_redaction`.
- `test_continuation_trace_has_state_sources_and_no_internal_labels`.
- `test_workflow_candidate_detection_for_repeated_successful_paths`.

## 12. Cambios De Código Sugeridos Sin Implementar

- Añadir tabla/JSONL `behavior_turn_receipts` o evento `turn_receipt` emitido al final de cada turno.
- Añadir campo `turn_id` a `observe_stream.payload`, `agent_tasks.metadata_json`, approval metadata y message artifacts.
- Cambiar extractor interno de costos para usar `usage` real por model.
- Añadir política explícita para KAIROS approvals: no autoaprobar approvals que no creó.
- Convertir `completed_unverified` en work queue de verificación.
- Crear un router de workflows separado de `bot.py` para patrones repetidos.
- Añadir policy tests para high-risk tools sin approval.
- Añadir memoria durable de preferencias con promoted facts y confidence thresholds.
- Añadir reportes automáticos semanales de behavior audit.
- Documentar criterios de “approval_was_necessary” como contrato evaluable.

## Evidencia, Inferencias Y Preguntas Abiertas

| Tipo | Detalle |
|---|---|
| Evidencia real | Conteos y casos vienen de SQLite read-only y approval JSON store sanitizado. |
| Inferencia | `approval_was_necessary`, `overcomplicated`, `underexecuted`, workflow candidates y missing/unnecessary permissions son heurísticos. |
| Pregunta abierta | Qué casos `completed_unverified` fueron aceptables para Hector aunque no tengan verifier pass. |
| Pregunta abierta | Qué workflows deben volverse producto versus permanecer como comportamiento conversacional. |
| Pregunta abierta | Qué datos deben guardarse como facts durables y cuáles solo como outcome learning. |
