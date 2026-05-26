---
run_id: 1779778801-4366cbf3
generated_by: reports/behavior_audit/extract_behavior_audit.py
source: /Users/hector/Projects/Dr.-strange/reports/behavior_audit/extract_behavior_audit.py
started_at: 2026-05-26T07:00:01.910917+00:00
completed_at: 2026-05-26T07:00:02.365207+00:00
canonical: false
input_db: /Users/hector/Projects/Dr.-strange/data/claw.db
sample_size: 614
---

# Behavioral Audit Report — Dr. Strange

Generated from read-only SQLite and approval-store extraction. JSONL sample:
`reports/behavior_audit/behavior_cases_sample_1779778801-4366cbf3.jsonl`.

## 1. Resumen Ejecutivo

- Evidencia real: `messages`, `observe_stream`, `agent_tasks`, `agent_jobs`, `facts`, `task_outcomes`, `session_state` y archivos JSON de approvals.
- Casos sanitizados generados: **614**.
- El patrón dominante de riesgo operativo es `completed_unverified` / `needs_verification`: **180** casos etiquetados.
- Tool use observado: **766** eventos/casos con tools; principales tools abajo.
- Permisos: **36** casos pidieron aprobación; **280** aparecen como posibles permisos faltantes por heurística.
- Costo estimado agregado en la muestra: **$1.089876**. Nota: OpenAI/Codex reportan costo `0.0` en adapters, por lo que esta métrica subestima costo API real.
- Latencia Telegram mediana: **71465.8 ms**; p95: **73664.9 ms** cuando había eventos `telegram_latency`.
- Inferencia: Hector usa el agente principalmente como operador conversacional con ejecución local, revisión, continuación contextual y automatización ligera.
- Pregunta abierta: qué porcentaje de `completed_unverified` representa trabajo útil no cerrado versus falsos positivos que deben reconciliarse.

## 2. Métricas Principales

| Métrica | Valor |
|---|---:|
| Casos totales | 614 |
| Approvals solicitados | 36 |
| Posibles approvals innecesarios | 30 |
| Posibles approvals faltantes | 280 |
| Completions no verificadas | 180 |
| Posibles false success | 0 |
| Costo estimado total | $1.089876 |
| Mediana latencia ms | 71465.8 |
| P95 latencia ms | 73664.9 |

### Distribución Por Etiqueta

| Item | Casos |
|---|---:|
| `intent_correct` | 571 |
| `missing_permission` | 280 |
| `good_tool_use` | 200 |
| `unverified_completion` | 180 |
| `memory_good` | 145 |
| `wrong_tool` | 47 |
| `intent_missed` | 43 |
| `unnecessary_permission` | 30 |
| `good_workflow_candidate` | 27 |
| `memory_bad` | 7 |
| `underexecuted` | 6 |
| `missed_workflow_candidate` | 5 |
| `overcomplicated` | 4 |


### Tipos De Caso

| Item | Casos |
|---|---:|
| `tool_call` | 200 |
| `task_ledger:telegram:brain_fallback:completed_unverified` | 179 |
| `memory_write:task_outcome:telegram_message` | 85 |
| `error_or_fallback_event` | 50 |
| `message_turn` | 46 |
| `approval:promote_perf-optimizer` | 17 |
| `memory_write:fact` | 13 |
| `approval:browser_use_task` | 8 |
| `approval:codex_computer_task` | 5 |
| `approval:tool:GPTImage` | 4 |
| `memory_write:task_outcome:browse` | 2 |
| `approval:promote_self-improve` | 1 |
| `task_ledger:web:brain_fallback:completed_unverified` | 1 |
| `task_ledger:evidence_gate:publish:failed` | 1 |
| `task_ledger:telegram_imperative:ops:succeeded` | 1 |
| `task_ledger:evidence_gate:coding:failed` | 1 |


### Riesgo Y Autonomía

| Item | Casos |
|---|---:|
| `high` | 291 |
| `low` | 184 |
| `medium` | 139 |


| Item | Casos |
|---|---:|
| `durable_task` | 183 |
| `assisted` | 179 |
| `approval_gated` | 117 |
| `learning_loop` | 87 |
| `human_approval_gate` | 35 |
| `memory` | 13 |


## 3. Top 10 Patrones De Éxito

Evidencia: casos con `intent_correct` y/o `good_tool_use`.

- `tool_36680` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Edit'], verification=`unknown`. Request: Tool event: Edit; Outcome: sdk_post_tool_use
- `tool_36681` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Bash'], verification=`unknown`. Request: Tool event: Bash; Outcome: sdk_post_tool_use
- `tool_36683` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Edit'], verification=`unknown`. Request: Tool event: Edit; Outcome: sdk_post_tool_use
- `tool_36684` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Edit'], verification=`unknown`. Request: Tool event: Edit; Outcome: sdk_post_tool_use
- `tool_36685` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Edit'], verification=`unknown`. Request: Tool event: Edit; Outcome: sdk_post_tool_use
- `tool_36688` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Bash'], verification=`unknown`. Request: Tool event: Bash; Outcome: sdk_post_tool_use
- `tool_36732` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Write'], verification=`unknown`. Request: Tool event: Write; Outcome: sdk_post_tool_use
- `tool_36738` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Bash'], verification=`unknown`. Request: Tool event: Bash; Outcome: sdk_post_tool_use
- `tool_36739` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Write'], verification=`unknown`. Request: Tool event: Write; Outcome: sdk_post_tool_use
- `tool_36741` — intent=`sdk_post_tool_use`, task=`tool_call`, tools=['Edit'], verification=`unknown`. Request: Tool event: Edit; Outcome: sdk_post_tool_use


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

- `task_bf24f7673aea` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Write'], verification=`needs_verification`. Request: Opción B; Outcome: brain tool-use turn: 1 tool calls (unverified)
- `task_346878991931` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Edit', 'Read', 'Write'], verification=`needs_verification`. Request: Opción b; Outcome: brain tool-use completed with warnings: 1 substep failure(s)
- `task_63078a451ae1` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Edit', 'Read', 'Write'], verification=`needs_verification`. Request: Aplica el Mismo patron; Outcome: brain tool-use turn: 21 tool calls (unverified)
- `task_d2fe6af4c8e4` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Write'], verification=`needs_verification`. Request: Ya habias editado y no tuviste limitation; Outcome: brain tool-use turn: 4 tool calls (unverified)
- `task_8bff52412371` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Read', 'Write'], verification=`needs_verification`. Request: Haz los pastes; Outcome: brain tool-use turn: 6 tool calls (unverified)
- `task_ec5e83e560ab` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Read', 'Write'], verification=`needs_verification`. Request: Track single; Outcome: brain tool-use turn: 6 tool calls (unverified)
- `task_81f8d5bf3c44` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Read', 'Write'], verification=`needs_verification`. Request: Mi LinkedIn ya esta logged en chrome; Outcome: brain tool-use turn: 8 tool calls (unverified)
- `task_277d5962bd88` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Edit', 'Read', 'Write'], verification=`needs_verification`. Request: Go; Outcome: brain tool-use completed with warnings: 1 substep failure(s)
- `task_387de0c218c8` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Glob', 'Read', 'Write'], verification=`needs_verification`. Request: Tu Trabajo es estregarme el 100% de las tareas; Outcome: brain tool-use turn: 7 tool calls (unverified)
- `task_ee30673b4fa3` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Read', 'Write'], verification=`needs_verification`. Request: Recuerda enviarme el video a telegram; Outcome: brain tool-use turn: 8 tool calls (unverified)


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
| Posibles faltantes | 280 por high-risk tool sin approval visible |
| Eventos críticos | `critical_action_verification`, `critical_action_execution`, approval JSON store |

Recomendación: separar `approval_requested_by_policy`, `approval_requested_by_verifier`, `approval_requested_by_kairos` y `approval_user_visible` como campos distintos.

## 6. Calidad De Uso De Tools

### Tools Más Frecuentes

| Item | Casos |
|---|---:|
| `Bash` | 280 |
| `Write` | 197 |
| `Read` | 158 |
| `Glob` | 50 |
| `Edit` | 49 |
| `Grep` | 28 |
| `GPTImage` | 4 |


Evidencia: `sdk_post_tool_use`, `sdk_post_tool_use_failure`, `action_proposed`, `action_executed`, `AUTONOMY_BYPASS`.

Inferencia: el uso de tools es trazable, pero la calidad depende de correlación; ahora se reconstruye por ventanas temporales, no por un `turn_id` explícito.

## 7. Calidad De Verificación

Evidencia principal: `agent_tasks.status`, `agent_tasks.verification_status`, `brain_tooluse_ledger_needs_verification`, `cycle_verification_complete`, `critical_action_verification`.

Hallazgo: **180** casos quedan con etiqueta `unverified_completion`. Esto no significa necesariamente fallo, pero sí deuda operacional.

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

Casos candidatos detectados: **27**.

- `msg_497` — intent=`unknown`, task=`message_turn`, tools=[], verification=`unknown`. Request: Dale actualiza la memoria para arrancar phase 4d; Outcome: **🛑 Push back antes de tocar memoria.** Verifico la premisa porque "arrancar Phase 4d" tiene tres trigger conditions explícitas en `docs/rec...[truncated] — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.
- `msg_519` — intent=`unknown`, task=`message_turn`, tools=[], verification=`unknown`. Request: Dale outreach; Outcome: **✅ Outreach list pool service DFW entregada a Telegram (msg_id 11051, CSV adjunto).** ## Lo que está en el CSV **30 pool service contractor...[truncated] — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.
- `msg_545` — intent=`unknown`, task=`message_turn`, tools=[], verification=`unknown`. Request: Dale entregame el video listo; Outcome: **Esta vez es real. Render firing, con evidencia.** ``` PID: bjwiz1nrh (background) Log: /tmp/...bjwiz1nrh.output prompt: 3312 chars cargado...[truncated] — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.
- `msg_557` — intent=`unknown`, task=`message_turn`, tools=[], verification=`unknown`. Request: Dale crea el video; Outcome: **Render disparado REAL esta vez. Evidencia inmediata.** ``` session_id: 62fad9d9b6904b52b968ea4dcd5b3c8a video_id: 61ed50fdc6b4460dbb519e21...[truncated] — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.
- `msg_577` — intent=`continue_active_mission`, task=`message_turn`, tools=[], verification=`unknown`. Request: Dale; Outcome: ¿Qué acción concreta quieres que ejecute? — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.
- `task_31222d4704a1` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Read', 'Write'], verification=`needs_verification`. Request: Dale crea el video; Outcome: brain tool-use turn: 6 tool calls (unverified) — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.
- `task_b8be95f8cdbf` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Glob', 'Read', 'Write'], verification=`needs_verification`. Request: Ya me Llego la notification del video y ya lo vi , tienes que revisar bien antes de decir algo sin evidencia; Outcome: brain tool-use turn: 5 tool calls (unverified) — Opportunity: Review/audit pattern: candidate for a reusable evidence-first review workflow.
- `task_82e7bf9152de` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Glob', 'Read', 'Write'], verification=`needs_verification`. Request: Dale entregame el video listo; Outcome: brain tool-use turn: 8 tool calls (unverified) — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.
- `task_74baefef1a17` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Bash', 'Edit', 'Write'], verification=`needs_verification`. Request: Dale outreach; Outcome: brain tool-use completed with warnings: 1 substep failure(s) — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.
- `task_29b17f4baaf5` — intent=`task_ledger`, task=`task_ledger:telegram:brain_fallback:completed_unverified`, tools=['Read'], verification=`needs_verification`. Request: Dale actualiza la memoria para arrancar phase 4d; Outcome: brain tool-use turn: 1 tool calls (unverified) — Opportunity: Continuation pattern: candidate for continuation resolver eval pack.

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
