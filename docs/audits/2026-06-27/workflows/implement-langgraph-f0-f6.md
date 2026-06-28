# Workflow de implementacion - LangGraph F0-F6

Fecha: 2026-06-27
Base: `docs/audits/2026-06-27/combined-verification.md` + plan validado contra documentacion LangGraph/SQLite.

## Objetivo

Atacar los faltantes del roadmap F0-F6 sin romper los invariantes ya buenos del repo:

- F0/F1 quedan como guardrails operativos.
- F2 pasa de "built, not deployed" a durabilidad activa y verificable.
- F3 deja de depender solo de `updated_at` y gana leases formales.
- F4-B2 agrega pausas humanas formales para evidencia insuficiente o acciones sensibles.
- F5 queda como subflujo browser verificable, no como atajo dentro del coordinator.
- F6 agrega fan-out/fan-in real para investigacion paralela.

## Postura arquitectonica

LangGraph debe ser el orquestador, no la nueva fuente de verdad.

Fuentes de verdad que se conservan:

- `RuntimeDb` / SQLite: estado durable, WAL y escrituras coordinadas.
- `TaskLedger` / `JobService`: ownership, estado operacional y recuperacion.
- `ToolRegistry` + `RuntimePolicyEngine`: unica entrada a herramientas.
- `ApprovalManager`: unica autoridad para aprobaciones humanas.
- `ObserveStream`: trazabilidad, auditoria y diagnostico.
- `CoordinatorService`: contrato externo hasta que el cutover este validado.

Reglas no negociables:

- No ejecutar herramientas mutantes fuera de `ToolRegistry`.
- No imprimir tokens, cookies, approval tokens ni secretos.
- No crear un segundo escritor SQLite.
- No saltarse Tier 3 ni approval gates mediante nodos LangGraph.
- No convertir `research`/`verifier` en lanes tool-capable; browser entra como subflujo controlado.

## Estado inicial

| Fase | Estado | Decision de workflow |
|---|---|---|
| F0 | Hecho | Usar como baseline de hygiene/observabilidad. |
| F1 | Hecho | Mantener single-writer y watchdog como invariante. |
| F2 | Built, not deployed | Activar por feature flag con checkpoint adapter. |
| F3 | Parcial | Agregar leases formales en `JobService`. |
| F4 | Parcial | Implementar B2 con interrupts + `ApprovalManager`. |
| F5 | Codigo en working tree; runtime default OFF via `browser_evidence_enabled`; pendiente deploy/live auth | Land primero; luego mover a subgraph formal. |
| F6 | No construido | Implementar fan-out/fan-in con limites de concurrencia. |

## Feature flags de cutover

PR-0 registra las flags antes de introducir LangGraph para que cada fase tenga rollback explicito:

| Campo `AppConfig` | Env principal | Env alias | Default | Alcance |
|---|---|---|---|---|
| `langgraph_shadow_enabled` | `CLAW_LANGGRAPH_SHADOW_ENABLED` | `LANGGRAPH_SHADOW_ENABLED` | `False` | Permite ejecutar el graph en shadow sin tomar trafico real. |
| `langgraph_coordinator_enabled` | `CLAW_LANGGRAPH_COORDINATOR_ENABLED` | `LANGGRAPH_COORDINATOR_ENABLED` | `False` | Permite cortar trafico al runner LangGraph cuando PR-7 lo habilite. |
| `browser_evidence_enabled` | `CLAW_BROWSER_EVIDENCE_ENABLED` | `BROWSER_EVIDENCE_ENABLED` | `False` | Permite omitir F5 y volver al coordinator legacy sin romper synthesis. |
| `f2_durability_enabled` | `CLAW_F2_DURABILITY_ENABLED` | `F2_DURABILITY_ENABLED` | `False` | Activa checkpoints F2 cuando PR-2 este validado. |
| `formal_job_leases_enabled` | `CLAW_FORMAL_JOB_LEASES_ENABLED` | `FORMAL_JOB_LEASES_ENABLED` | `False` | Activa leases formales en `JobService` para claims, heartbeats y reclaim por expiracion. |

## Secuencia de PRs

### PR-0 - Baseline y contrato de cutover

Proposito: congelar el punto de partida antes de meter LangGraph.

Estado PR-0: implementado en working tree. Las flags existen en `AppConfig`, todas son default-off, y F5 browser evidence queda protegido por `browser_evidence_enabled`.

Cambios esperados:

- Documentar flags previstas: `langgraph_shadow_enabled`, `langgraph_coordinator_enabled`, `f2_durability_enabled`, `browser_evidence_enabled`.
- Actualizar el roadmap local para distinguir "codigo en working tree" vs "deploy/live validated".
- Agregar un test de contrato que confirme que el coordinator actual sigue funcionando con LangGraph apagado.

Gates:

```bash
uvx ruff check claw_v2 tests
uvx ruff format --check claw_v2 tests
.venv/bin/python -m pytest tests/test_task_handler.py tests/test_coordinator.py -q
```

Criterio de salida:

- Con todas las flags apagadas, el comportamiento observable es identico al actual.

### PR-1 - LangGraph skeleton en modo shadow

Proposito: introducir LangGraph sin tomar trafico real.

Estado PR-1: implementado en working tree. `CoordinatorService` sigue siendo la API publica; el runner shadow se construye solo con `langgraph_shadow_enabled=True`, compara el resultado legacy sin reemplazarlo y emite eventos `langgraph_shadow_started`, `langgraph_shadow_completed` o `langgraph_shadow_failed`. En el entorno actual `langgraph` no esta instalado, asi que el modulo usa un backend determinista local y mantiene compatibilidad opcional con `langgraph.graph.StateGraph` cuando la dependencia exista.

Cambios esperados:

- Crear un modulo local, por ejemplo `claw_v2/langgraph_coordinator.py`.
- Modelar nodos equivalentes a las fases actuales: intake, research, synthesis, verification, finalization.
- Exponer un runner shadow que reciba el mismo input del `CoordinatorService` y produzca un reporte comparativo, sin mutar estado.
- Emitir eventos `langgraph_shadow_started`, `langgraph_shadow_completed`, `langgraph_shadow_failed`.

Patron:

- `CoordinatorService` sigue siendo la API publica.
- El graph se invoca solo si `langgraph_shadow_enabled=True`.
- El resultado shadow nunca reemplaza el resultado productivo.

Gates:

```bash
.venv/bin/python -m pytest tests/test_coordinator.py -q
.venv/bin/python -m pytest tests/test_runtime_policy.py tests/test_tool_policy.py -q
```

Criterio de salida:

- Shadow graph corre deterministamente en tests y no ejecuta herramientas ni escribe estado durable.

### PR-2 - F2 checkpoint adapter

Proposito: conectar checkpoints LangGraph al modelo durable existente.

Estado PR-2: implementado en working tree. El shadow runner acepta un `LangGraphF2CheckpointAdapter` que usa el `F2DurabilityStore` existente, por lo que comparte el `RuntimeDb` unico y no abre otro writer SQLite. El adapter se construye solo cuando `langgraph_shadow_enabled=True` y `f2_durability_enabled=True`; mapea `thread_id == task_id` y guarda cada nodo en la fase F2 `langgraph_shadow:{node}`. Los nodos ya confirmados como `succeeded` se saltan en una ejecucion posterior para reanudar despues de fallos entre nodos.

Cambios esperados:

- Crear un adapter de checkpoints respaldado por `RuntimeDb`, o encapsular un checkpointer SQLite sin violar el single-writer.
- Mapear `thread_id` a `task_id` y namespace a fase/nodo.
- Persistir checkpoints antes/despues de nodos con side effects.
- Agregar recuperacion despues de fallo sintetico entre nodos.

Regla de diseno:

- El adapter no debe abrir una conexion SQLite independiente que compita con `RuntimeDb` sin una decision explicita y probada de concurrencia.

Gates:

```bash
.venv/bin/python -m pytest tests/test_f2_resume_recovery.py tests/test_f2_phase_checkpoint_writes.py -q
.venv/bin/python -m pytest tests/test_task_handler.py -q
```

Criterio de salida:

- Una tarea interrumpida despues de un checkpoint se reanuda desde el ultimo nodo confirmado, no desde cero.

### PR-3 - F3 leases formales

Proposito: hacer explicita la ownership de tareas largas.

Estado PR-3: implementado y corregido en working tree detras de `formal_job_leases_enabled`. `JobService` agrega columnas de lease (`lease_owner`, `lease_expires_at`, `lease_heartbeat_at`, `lease_generation`) con migracion pasiva para DBs existentes. Cuando el flag esta activo, `claim`/`claim_next` adquieren leases atomicos, incrementan `lease_generation` y solo emiten exito si el `UPDATE` afecta exactamente una fila. `lease_generation` es el token CAS obligatorio: `worker_id` solo no basta. `heartbeat_lease`, `release_lease` y las mutaciones formales de lifecycle (`checkpoint`, `wait_for_approval`, `complete`, `fail`, `reschedule`) requieren `lease_owner` + `lease_generation` vigentes para mutar. `recover_stale_running` usa expiracion de lease cuando el modo formal esta activo. No se agregaron nuevos writers ni conexiones `RuntimeDb`.

Decision de parada: PR-4 no debe iniciarse hasta conservar este estado GO/GO WITH RISKS del PR-3 fix bajo los gates listados abajo. El flag `formal_job_leases_enabled` sigue default-off; no habilitarlo en runtime real hasta que los runners pasen `lease_owner` + `lease_generation` y, si aplica, hagan heartbeat.

Cambios esperados:

- Agregar campos de lease a la tabla operacional correspondiente: owner, lease_expires_at, lease_heartbeat_at, lease_generation.
- Implementar acquire, heartbeat, release y reclaim atomicos en `JobService`.
- Hacer que los runners durables usen lease antes de ejecutar trabajo.
- Emitir eventos de lease acquire/release/reclaim.

Gates:

```bash
.venv/bin/python -m py_compile claw_v2/jobs.py claw_v2/config.py claw_v2/main.py tests/test_jobs.py tests/test_config.py tests/test_architecture_invariants.py
uvx ruff check claw_v2 tests
uvx ruff format --check claw_v2 tests
.venv/bin/python -m pytest tests/test_jobs.py tests/test_config.py tests/test_task_handler.py tests/test_sqlite_runtime.py tests/test_observe_subscribe.py tests/test_runtimedb_wiring.py tests/test_architecture_invariants.py -q
git diff --check
```

Criterio de salida:

- Dos workers no pueden ejecutar el mismo job activo.
- Un worker caido libera trabajo solo despues de expirar lease.
- Un worker tardio con generacion vieja no puede hacer heartbeat, release, checkpoint, approval, complete, fail ni reschedule sobre una generacion nueva.
- Las rutas legacy con `formal_job_leases_enabled=False` conservan comportamiento anterior.

### PR-4 - F6 fan-out/fan-in

Proposito: reemplazar el coordinator fijo de 4 fases por investigacion paralela controlada.

Cambios esperados:

- Agregar un nodo planner que produce unidades de investigacion.
- Usar fan-out dinamico para research workers con limite de concurrencia.
- Reducir resultados con orden estable y metadata de procedencia.
- Mantener verifier como fase separada y sin herramientas directas.
- Registrar por worker: input, evidence summary, status, error, timing.

Gates:

```bash
.venv/bin/python -m pytest tests/test_coordinator.py tests/test_browser_evidence.py -q
.venv/bin/python -m pytest tests/test_llm.py tests/test_bot_helpers.py -q
```

Criterio de salida:

- N workers producen un evidence pack estable.
- Un worker fallido no corrompe el fan-in; queda como error trazable.

### PR-5 - F5 browser subgraph

Proposito: formalizar browser evidence como capacidad verificable dentro del graph.

Cambios esperados:

- Mover la integracion actual de `BrowserEvidenceCollector` a un nodo/subgraph F5.
- Mantener ejecucion exclusivamente por `ToolRegistry`.
- Separar herramientas read-only (`BrowserNavigate`, `BrowserSnapshot`) de acciones mutantes (`BrowserClick`, `BrowserType`, etc.).
- Agregar preflight de sesion para sitios que requieran login, reportando "auth missing" como evidencia negativa, no como exito.
- Incluir contenido browser en el evidence pack antes de synthesis.

Gates:

```bash
.venv/bin/python -m pytest tests/test_browser_evidence.py tests/test_coordinator.py -q
.venv/bin/python -m pytest tests/test_tools.py tests/test_runtime_policy.py tests/test_tool_policy.py -q
```

Criterio de salida:

- Una tarea con URLs produce snapshot redacted en evidence pack.
- Una pagina no autenticada falla honestamente y no inventa contenido.
- Ninguna herramienta browser mutante corre sin approval.

### PR-6 - F4-B2 human-in-the-loop formal

Proposito: pausar y reanudar tareas cuando falta aprobacion o evidencia suficiente.

Cambios esperados:

- Usar interrupts/pause points en el graph para decision humana.
- Delegar la autoridad real a `ApprovalManager`.
- Persistir el estado de pausa via F2.
- Reanudar por task/session id sin depender de memoria de chat.
- Agregar eventos `human_input_required`, `human_input_resumed`, `human_input_expired`.

Gates:

```bash
.venv/bin/python -m pytest tests/test_approval*.py tests/test_task_handler.py -q
.venv/bin/python -m pytest tests/test_tool_policy.py tests/test_runtime_policy.py -q
```

Criterio de salida:

- Un flujo Tier 3 se pausa, solicita aprobacion, reanuda con token valido y rechaza replay.

### PR-7 - Cutover gradual

Proposito: pasar trafico del coordinator actual al LangGraph runner sin salto ciego.

Pasos:

1. Shadow mode por defecto en entorno local.
2. Canary con `langgraph_coordinator_enabled=True` solo para tareas de bajo riesgo.
3. Comparacion de resultados contra coordinator legacy.
4. Ampliar a tareas browser/research.
5. Dejar legacy disponible por flag de rollback.

Gates:

```bash
.venv/bin/python -m pytest tests/ -q
```

Criterio de salida:

- Full suite verde o con flakes documentados y reproducidos aisladamente.
- `observe_stream` muestra eventos de graph, checkpoint, lease y browser sin secretos.
- Rollback por flag probado.

## Matriz de pruebas minima

| Area | Pruebas requeridas |
|---|---|
| F2 checkpoints | Reanudacion post-fallo, idempotencia, checkpoint corrupto, flag off. |
| F3 leases | Contencion, expiracion, heartbeat, reclaim, doble worker. |
| F4-B2 | Pause/resume, approval replay, timeout, rechazo humano. |
| F5 browser | URL extraction, current page snapshot, auth missing, policy denial, redaction. |
| F6 fan-out | Orden estable, worker parcial fallido, limites de concurrencia, reducer determinista. |
| Seguridad | Tier 3, no secrets in observe, no direct tool bypass, no raw browser mutation. |

## Checklist por PR

- Leer archivos afectados antes de editar.
- Agregar test rojo o contrato de regresion antes del cambio riesgoso.
- Mantener feature flag default-off cuando el cambio altere runtime.
- Emitir eventos observables sin datos sensibles.
- Actualizar docs de roadmap si cambia el estado F0-F6.
- Ejecutar ruff check, ruff format check y tests focales.
- Registrar cualquier flake con reproduccion aislada.

## Criterios de bloqueo

Bloquear el PR si ocurre cualquiera de estos puntos:

- El graph ejecuta una herramienta sin pasar por `ToolRegistry`.
- Aparece un segundo writer SQLite no coordinado.
- Un approval token queda en logs, tests, observe o exceptions.
- F2 no puede reanudar despues de un fallo sintetico.
- F5 produce contenido sintetico cuando browser/auth falla.
- F6 fan-in es no determinista.

## Rollback

Cada PR que cambia runtime debe tener rollback por flag:

- `langgraph_shadow_enabled=False` apaga shadow.
- `langgraph_coordinator_enabled=False` vuelve al coordinator legacy.
- `f2_durability_enabled=False` desactiva checkpoints nuevos.
- `browser_evidence_enabled=False` omite F5 sin romper synthesis.

Rollback aceptable:

- Conserva datos ya escritos.
- No requiere migracion destructiva.
- Deja un evento observable indicando que el modo fue desactivado.

## Referencias externas de validacion

- LangGraph overview: https://docs.langchain.com/oss/python/langgraph/overview
- Graph API: https://docs.langchain.com/oss/python/langgraph/graph-api
- Persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- Checkpointers: https://docs.langchain.com/oss/python/langgraph/checkpointers
- Fault tolerance: https://docs.langchain.com/oss/python/langgraph/fault-tolerance
- Interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- Subgraphs: https://docs.langchain.com/oss/python/langgraph/use-subgraphs
- Workflows and agents: https://docs.langchain.com/oss/python/langgraph/workflows-agents
- Backward compatibility: https://docs.langchain.com/oss/python/langgraph/backward-compatibility
- Testing: https://docs.langchain.com/oss/python/langgraph/test
- SQLite WAL: https://sqlite.org/wal.html
