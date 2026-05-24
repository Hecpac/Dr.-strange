# Hito 2026-05-24 — Turn ID Correlator + Wave-1 Behavior Audit Fixes

Resumen ejecutivo del trabajo entregado entre 2026-05-23 y 2026-05-24.
Recoge contexto, alcance, métricas pre/post y enlaces para que un futuro
operador (humano o agente) entienda *por qué* el daemon ahora correlaciona
turnos por una sola columna.

## Anchor

- **Origen**: behavioral audit del 2026-05-23 (`reports/behavior_audit/BEHAVIOR_AUDIT_REPORT.md`, 524 cases canónicos + 100 cases del complemento Claude Code).
- **PRs**:
  - `feat/p0-p1-audit-fixes` (mergeado a main local como `53aa929`, llegó a remoto vía PR #36 como parte del scope).
  - **PR #36** — `feat(bot): open turn_id_context in BotService.handle_text` (mergeado a `origin/main` como `da70535`).
- **Issue follow-up**: #37 — race condition pre-existente en `task_ledger.mark_stale_running_lost` (no es de este hito; reportado por Codex review).
- **Commits clave**:
  - `53aa929` — wave-1 audit fixes (D, F, E, G, A, C, B) + pre-merge review blockers
  - `48e8bd8` — handle_text abre `turn_id_context`; `task_ledger_created` reactivado en CRITICAL set
  - `da70535` — merge commit en origin/main

## Por qué importaba

El reporte de auditoría señaló cinco síntomas operacionales agudos:

1. **91 tasks `completed_unverified`** sin reconciliación ni SLA.
2. **99/100 brain-tooluse rows con `channel=NULL`** aunque el `task_id` era `tg-*`.
3. **144 `task_outcomes` con `outcome='success'`** mientras 91 ledger rows quedaban unverified — divergencia de sistemas paralelos.
4. **6+ `soul_update_suggestion` duplicados** en 4 días por falta de dedup.
5. **17 pending approvals `promote_perf-optimizer`** acumulados por un handler sin backpressure.
6. Sumado: **sin `turn_id` global**, cualquier post-mortem dependía de ventanas temporales frágiles entre `messages → dispatch → tools → task_ledger → approval → memory`.

Además, el extractor canónico y la corrida del complemento Claude Code se sobrescribieron mutuamente porque ambos usaban `behavior_cases_sample.jsonl` sin lock.

## Alcance entregado

| Letra | Objetivo | Implementación |
|---|---|---|
| **A** | Output-safety en behavior audit | `claw_v2/behavior_audit_io.py`: `run_id`, `O_EXCL` canonical, YAML frontmatter en cada report |
| **B** | turn_id global como correlador | `claw_v2/turn_context.py` (ContextVar); auto-inject en `ObserveStream.emit`, `TaskLedger.create`, `ApprovalManager.create`; `turn_id_missing` sibling para eventos críticos sin contexto |
| **C** | Reconciliación de `completed_unverified` | `claw_v2/reconciliation.py` (dry-run, pure read) + `scripts/audit/reconcile_completed_unverified.py` (CLI) + evento `pending_verification_reconciliation` |
| **D** | Canal Telegram en brain-fallback rows | `BotService._attach_brain_tool_use_ledger` pasa `route={channel, external_session_id}` derivado de `runtime_channel` o prefijo de session |
| **E** | `task_outcomes` alineado con ledger | Schema extendido con `usable_reply_unverified` (migración crash-safe), helper `_classify_brain_outcome_value`, reorder en `_brain_text_response` para clasificar tras el ledger attach |
| **F** | Dedup de `soul_update_suggestion` | Key pasa de timestamp a `content_hash`; `MemoryStore.bump_fact_confidence` para consolidar repeticiones; evento `learning_loop_dedup` |
| **G** | Backpressure self-improve | `claw_v2/self_improve_backpressure.py`: cuenta pending por `promote_<agent>`, pausa cuando excede umbral; `_self_improve_handler` consulta antes de cada run; evento `self_improve_paused_backlog_too_high` |
| **P2** | Activar el correlator | `BotService.handle_text` envuelve cada turno en `turn_id_context(new_turn_id())`; `task_ledger_created` reactivado en `CRITICAL_OBSERVE_EVENTS_REQUIRING_TURN_ID` |

### Bloqueantes detectados en review pre-merge

Tres bugs introducidos durante la wave-1 fueron capturados antes del merge:

- `_self_improve_handler` referenciaba `approvals` no presente en el scope de `_setup_scheduler` (NameError al primer cron tick). Fix: añadir `approvals: ApprovalManager` al param list + pasarlo en el call site.
- Migración de `task_outcomes` no era atómica: si el proceso moría entre `RENAME` y `CREATE/INSERT`, en el siguiente boot el extractor creaba la tabla nueva y el `_ensure_*` retornaba early — pérdida silenciosa de filas. Fix: `BEGIN IMMEDIATE` + detección de `task_outcomes_old` huérfana + resume con verificación count lossless.
- `task_ledger_created` en `CRITICAL_OBSERVE_EVENTS_REQUIRING_TURN_ID` antes de que `handle_text` abriera el context iba a generar `turn_id_missing` flood en el primer boot. Fix: diferir hasta P2.

## Métricas pre / post

| Indicador | Pre-merge (audit 2026-05-23) | Post-restart (2026-05-24, primera ventana de uso real) |
|---|---|---|
| `agent_tasks.channel = NULL` en brain-fallback tg-* | **99 / 100** | **0 / 7** (todas con `channel='telegram'`) |
| `task_outcomes.outcome='success'` con status `completed_unverified` | divergencia 144 vs 91 | **0** (los 7 turnos con tools quedan `usable_reply_unverified`; los 4 puros chat siguen `success`) |
| `turn_id` distintos correlacionando un turno | **0** (sin correlator) | **11** (uno por turno) |
| Eventos críticos con `turn_id` poblado | **0** | **394** sobre los 11 turnos |
| `turn_id_missing` events (gap visible) | n/a | **0** (todo handle_text path cubierto) |
| `dispatch_decision` con turn_id | **0%** | **150 / 150 = 100%** |
| `brain_turn_start` / `brain_turn_complete` con turn_id | **0%** | **11 / 11 = 100%** |
| Tests | 2031 passed | **2079 passed**, 6 xfailed, 215 subtests (+44 nuevos, 0 regresiones) |

## Decisiones de diseño

- **ContextVar en módulo dedicado** (`claw_v2/turn_context.py`) en lugar de `claw_v2/bot_helpers.py` para evitar cycles cuando lo importan `observe.py`, `task_ledger.py`, `approval.py`.
- **`handle_text` split en wrapper + `_handle_text_body`** en lugar de indentar 1100 líneas. Cero cambio de lógica; el wrapper hace auth + `with turn_id_context(...)` y delega. Preserva el invariante `evidence_gate_meta_skip_sync_path` (sigue siendo síncrono).
- **O_EXCL para canonical, run-suffixed para per-run** en behavior audit: si dos extractores corren en paralelo, el segundo no clobbera al primero; cada uno mantiene su artifact.
- **Backpressure por `action_kind` derivado del agent name**, no por una lista hardcoded. Permite que self-improve genere nuevos agentes sin tocar la regla.
- **Schema migration con `BEGIN IMMEDIATE`** + verificación de count lossless antes de `DROP TABLE old`. Resume desde estado interrumpido en reboot.

## Cosas que NO se hicieron (y por qué)

- **`bot.py:4454` (evidence_gate) y `bot.py:7574` (telegram_imperative ops)** siguen con `channel=NULL` para sus tasks. Wave-1 fix D solo cubrió brain-fallback. Plan: aplicar el mismo patrón surgicalmente en un PR independiente cuando se consolide la siguiente wave.
- **Backfill de los 144 `task_outcomes` históricos** quedó intencionalmente sin tocar — la regla del user fue "no modifies data/claw.db directamente". Si en el futuro se quiere reconciliar, requiere un script de backfill que cruce con `agent_tasks.status` y reclasifique retroactivamente.
- **`_lookup_recent_brain_tool_use_record` con ventana 60s** sigue siendo vulnerable a turnos extremos (e.g. el `task_17fe13cf5f76` con 19 tool calls que cita el audit). Pendiente: ampliar la ventana o filtrar por `trace_id` en lugar de timestamp.

## Pendientes inmediatos

1. **Auditoría post-turn_id (24–48h de uso)** — re-correr el extractor cuando haya volumen suficiente y comparar contra el baseline pre-merge.
2. **`behavior_turn_receipt`** — sintetizar un receipt por turno con `turn_id, session_id, user_text_hash, intent, tools, approvals_requested, evidence_manifest_ref, verification_status, outcome, duration_ms` para que el agente vea su propio comportamiento como dato estructurado.
3. **Approvals semánticos** — `risk_basis, requested_by, visible_to_user, resolved_by` (recomendación R3 del reporte canónico).
4. **Capability Grants** — sustituir el `autonomy_grant` ad-hoc por grants explícitos con scope (tool/domain/path), grantor, TTL, revocables.
5. **Issue #37** — race en `mark_stale_running_lost` (no de esta wave, pero observable).

## Cómo verificar el correlator en producción

```sql
-- todos los eventos de un turno (a través de observe + ledger):
SELECT 'observe' AS source, event_type, datetime(timestamp) AS ts
FROM observe_stream
WHERE json_extract(payload, '$.turn_id') = :turn_id
UNION ALL
SELECT 'agent_tasks', task_id, datetime(created_at, 'unixepoch')
FROM agent_tasks
WHERE json_extract(metadata_json, '$.turn_id') = :turn_id
ORDER BY ts DESC;
```

Para encontrar turnos recientes con cobertura:

```sql
SELECT DISTINCT json_extract(payload, '$.turn_id') AS turn_id,
       MIN(datetime(timestamp)) AS started_at,
       COUNT(*) AS events
FROM observe_stream
WHERE json_extract(payload, '$.turn_id') IS NOT NULL
GROUP BY turn_id
ORDER BY started_at DESC LIMIT 20;
```
