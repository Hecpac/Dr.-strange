---
run_id: 1779692402-97110d3c
generated_by: reports/behavior_audit/extract_behavior_audit.py
source: /Users/hector/Projects/Dr.-strange/reports/behavior_audit/extract_behavior_audit.py
started_at: 2026-05-25T07:00:02.198837+00:00
completed_at: 2026-05-25T07:00:02.661843+00:00
canonical: false
input_db: /Users/hector/Projects/Dr.-strange/data/claw.db
sample_size: 609
---

# Behavioral Audit Report — Dr. Strange

Generated from read-only SQLite and approval-store extraction. JSONL sample:
`reports/behavior_audit/behavior_cases_sample_1779692402-97110d3c.jsonl`.

## 1. Resumen Ejecutivo

- Evidencia real: `messages`, `observe_stream`, `agent_tasks`, `agent_jobs`, `facts`, `task_outcomes`, `session_state` y archivos JSON de approvals.
- Casos sanitizados generados: **609**.
- El patrón dominante de riesgo operativo es `completed_unverified` / `needs_verification`: **127** casos etiquetados.
- Tool use observado: **618** eventos/casos con tools; principales tools abajo.
- Permisos: **35** casos pidieron aprobación; **235** aparecen como posibles permisos faltantes por heurística.
- Costo estimado agregado en la muestra: **$16.002008**. Nota: OpenAI/Codex reportan costo `0.0` en adapters, por lo que esta métrica subestima costo API real.
- Latencia Telegram mediana: **76516.1 ms**; p95: **248532.2 ms** cuando había eventos `telegram_latency`.
- Inferencia: Hector usa el agente principalmente como operador conversacional con ejecución local, revisión, continuación contextual y automatización ligera.
- Pregunta abierta: qué porcentaje de `completed_unverified` representa trabajo útil no cerrado versus falsos positivos que deben reconciliarse.

## 2. Métricas Principales

| Métrica | Valor |
|---|---:|
| Casos totales | 609 |
| Approvals solicitados | 35 |
| Posibles approvals innecesarios | 30 |
| Posibles approvals faltantes | 235 |
| Completions no verificadas | 127 |
| Posibles false success | 0 |
| Costo estimado total | $16.002008 |
| Mediana latencia ms | 76516.1 |
| P95 latencia ms | 248532.2 |

### Distribución Por Etiqueta

| Item | Casos |
|---|---:|
| `intent_correct` | 517 |
| `missing_permission` | 235 |
| `good_tool_use` | 194 |
| `memory_good` | 189 |
| `unverified_completion` | 127 |
| `intent_missed` | 92 |
| `wrong_tool` | 51 |
| `unnecessary_permission` | 30 |
| `memory_bad` | 25 |
| `good_workflow_candidate` | 18 |
| `underexecuted` | 15 |
| `overcomplicated` | 9 |
| `missed_workflow_candidate` | 8 |


### Tipos De Caso

| Item | Casos |
|---|---:|
| `tool_call` | 200 |
| `task_ledger:telegram:brain_fallback:completed_unverified` | 127 |
| `message_turn` | 94 |
| `memory_write:task_outcome:telegram_message` | 85 |
| `error_or_fallback_event` | 50 |
| `approval:promote_perf-optimizer` | 17 |
| `memory_write:fact` | 10 |
| `approval:browser_use_task` | 7 |
| `approval:codex_computer_task` | 5 |
| `approval:tool:GPTImage` | 4 |
| `memory_write:task_outcome:browse` | 3 |
| `memory_write:task_outcome:critical_action` | 2 |
| `approval:promote_self-improve` | 1 |
| `task_ledger:evidence_gate:publish:failed` | 1 |
| `task_ledger:telegram_imperative:ops:succeeded` | 1 |
| `task_ledger:evidence_gate:coding:failed` | 1 |


### Riesgo Y Autonomía

| Item | Casos |
|---|---:|
| `high` | 251 |
| `low` | 228 |
| `medium` | 130 |


| Item | Casos |
|---|---:|
| `assisted` | 217 |
| `durable_task` | 131 |
| `approval_gated` | 127 |
| `learning_loop` | 90 |
| `human_approval_gate` | 34 |
| `memory` | 10 |


## 3. Top 10 Patrones De Éxito

Evidencia: casos con `intent_correct` y/o `good_tool_use`.

- `tool_30228` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Grep'], verification=`unknown`. Request: Tool event: Grep; Outcome: sdk_post_tool_use
- `tool_30229` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Grep'], verification=`unknown`. Request: Tool event: Grep; Outcome: sdk_post_tool_use
- `tool_30230` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Read'], verification=`unknown`. Request: Tool event: Read; Outcome: sdk_post_tool_use
- `tool_30231` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Grep'], verification=`unknown`. Request: Tool event: Grep; Outcome: sdk_post_tool_use
- `tool_30235` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Bash'], verification=`unknown`. Request: Tool event: Bash; Outcome: sdk_post_tool_use
- `tool_30236` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Bash'], verification=`unknown`. Request: Tool event: Bash; Outcome: sdk_post_tool_use
- `tool_30237` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Read'], verification=`unknown`. Request: Tool event: Read; Outcome: sdk_post_tool_use
- `tool_30238` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Read'], verification=`unknown`. Request: Tool event: Read; Outcome: sdk_post_tool_use
- `tool_30239` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Read'], verification=`unknown`. Request: Tool event: Read; Outcome: sdk_post_tool_use
- `tool_30240` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Read'], verification=`unknown`. Request: Tool event: Read; Outcome: sdk_post_tool_use


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

- `task_11a595591d14` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Grep'], verification=`needs_verification`. Request: Revisa si se applicaron los cambios; Outcome: brain tool-use turn: 5 tool calls (unverified)
- `task_f94f0efcdb44` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Edit', 'Grep', 'Read', 'Write'], verification=`needs_verification`. Request: Activa schedule job de echo; Outcome: brain tool-use turn: 11 tool calls (unverified)
- `task_02086f6a0ec9` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Edit', 'Read', 'Write'], verification=`needs_verification`. Request: Promueve el script; Outcome: brain tool-use completed with warnings: 2 substep failure(s)
- `task_1a9597b2a8b1` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Edit', 'Grep', 'Read'], verification=`needs_verification`. Request: Investiga y déjalo operativo; Outcome: brain tool-use turn: 13 tool calls (unverified)
- `task_aa016eb3b6a1` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Grep', 'Read'], verification=`needs_verification`. Request: Vuelve a probar el agente; Outcome: brain tool-use turn: 10 tool calls (unverified)
- `task_4658200cea35` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Edit', 'Glob', 'Grep', 'Read', 'Write'], verification=`needs_verification`. Request: Verifica el path y haz los fixes de agente echo; Outcome: brain tool-use completed with warnings: 2 substep failure(s)
- `task_3633d08f767a` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Edit', 'Grep', 'Read', 'Write'], verification=`needs_verification`. Request: Verificaste ?; Outcome: brain tool-use completed with warnings: 2 substep failure(s)
- `task_e911f5573658` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Grep', 'Read'], verification=`needs_verification`. Request: Investiga el path; Outcome: brain tool-use turn: 8 tool calls (unverified)
- `task_b2b2a398af70` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Edit', 'Glob', 'Grep', 'Read', 'Write'], verification=`needs_verification`. Request: Prueba el agente para que revise instagram; Outcome: brain tool-use completed with warnings: 1 substep failure(s)
- `task_dac1b6615ac0` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Write'], verification=`needs_verification`. Request: Vamos con tu pick; Outcome: brain tool-use turn: 7 tool calls (unverified)


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
| Approvals en muestra | 35 casos |
| Posibles innecesarios | 30 por heurística low-risk/read-only |
| Posibles faltantes | 235 por high-risk tool sin approval visible |
| Eventos críticos | `critical_action_verification`, `critical_action_execution`, approval JSON store |

Recomendación: separar `approval_requested_by_policy`, `approval_requested_by_verifier`, `approval_requested_by_kairos` y `approval_user_visible` como campos distintos.

## 6. Calidad De Uso De Tools

### Tools Más Frecuentes

| Item | Casos |
|---|---:|
| `Bash` | 235 |
| `Read` | 130 |
| `Write` | 124 |
| `Grep` | 54 |
| `Edit` | 36 |
| `Glob` | 34 |
| `GPTImage` | 4 |
| `coordinator.autonomous_task` | 1 |


Evidencia: `sdk_post_tool_use`, `sdk_post_tool_use_failure`, `action_proposed`, `action_executed`, `AUTONOMY_BYPASS`.

Inferencia: el uso de tools es trazable, pero la calidad depende de correlación; ahora se reconstruye por ventanas temporales, no por un `turn_id` explícito.

## 7. Calidad De Verificación

Evidencia principal: `agent_tasks.status`, `agent_tasks.verification_status`, `brain_tooluse_ledger_needs_verification`, `cycle_verification_complete`, `critical_action_verification`.

Hallazgo: **127** casos quedan con etiqueta `unverified_completion`. Esto no significa necesariamente fallo, pero sí deuda operacional.

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

Casos candidatos detectados: **18**.

- `msg_247` — intent=`unknown`, task=`message_turn`, tools=[], verification=`unknown`. Request: Dame el prompt para crear las imágenes y te las paso , otra cosa no creaste el cuaderno en NotebookLM; Outcome: Dos cosas — primero corrijo lo del cuaderno (tenías razón, fallé en mi reporte anterior), después el prompt de imágenes. ## ✅ Cuaderno Noteb...[truncated] — Opportunity: NotebookLM request pattern: candidate for a typed notebook workflow with approval boundaries.
- `msg_373` — intent=`unknown`, task=`message_turn`, tools=[], verification=`unknown`. Request: Dale seguimiento hasta que finalice el video; Outcome: **📊 Estado dual — un video listo, otro renderizando:** **(1) Sycophancy — COMPLETADO ✅** - `video_id`: `1bfc4e3f061a40cc9d45aa078030e5e2` - ...[truncated] — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.
- `task_11a595591d14` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Grep'], verification=`needs_verification`. Request: Revisa si se applicaron los cambios; Outcome: brain tool-use turn: 5 tool calls (unverified) — Opportunity: Review/audit pattern: candidate for a reusable evidence-first review workflow.
- `task_3d3f2bc1e4bc` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Read', 'Write'], verification=`needs_verification`. Request: Dale seguimiento hasta que finalice el video; Outcome: brain tool-use turn: 14 tool calls (unverified) — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.
- `task_129ae36837b2` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Grep', 'Read'], verification=`needs_verification`. Request: Buscalo y Abrelo para revisar lo en la Mac; Outcome: brain tool-use completed with warnings: 2 substep failure(s) — Opportunity: Review/audit pattern: candidate for a reusable evidence-first review workflow.
- `task_40713f59c3e6` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Read', 'Write'], verification=`needs_verification`. Request: Commenta y revisa el repo; Outcome: brain tool-use turn: 5 tool calls (unverified) — Opportunity: Review/audit pattern: candidate for a reusable evidence-first review workflow.
- `task_7b03b6e56cc6` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Read', 'Write'], verification=`needs_verification`. Request: Dame el prompt para crear las imágenes y te las paso , otra cosa no creaste el cuaderno en NotebookLM; Outcome: brain tool-use turn: 6 tool calls (unverified) — Opportunity: NotebookLM request pattern: candidate for a typed notebook workflow with approval boundaries.
- `task_0aebc514d732` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Write'], verification=`needs_verification`. Request: Revisa este posts https://www.instagram.com/p/DYZ0ptyAAIy/?igsh=eWtmOXk2eHphZjZo; Outcome: brain tool-use turn: 2 tool calls (unverified) — Opportunity: Review/audit pattern: candidate for a reusable evidence-first review workflow.
- `task_74eb6498bead` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Read', 'Write'], verification=`needs_verification`. Request: Crea el cuaderno; Outcome: brain tool-use turn: 5 tool calls (unverified) — Opportunity: NotebookLM request pattern: candidate for a typed notebook workflow with approval boundaries.
- `task_9a7d88d25b0f` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Write'], verification=`needs_verification`. Request: Revisa el sistema de la Mac el ram y las apps Ultimamente al abrir el CLI de Claude code esta superlento y muchas veces ...[truncated]; Outcome: brain tool-use turn: 2 tool calls (unverified) — Opportunity: Review/audit pattern: candidate for a reusable evidence-first review workflow.

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
