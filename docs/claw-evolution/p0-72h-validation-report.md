# P0 Telemetry — Validación 72h

**Período:** 29-abr 22:39 → 2-may 15:24 (~72h)
**Volumen:** 94 goals + 239 claims + 352 events = 685 líneas (394 KiB / 403,723 bytes)

## Resumen ejecutivo

La telemetría P0 activa en `~/.claw/telemetry/` tiene el volumen esperado y los tres archivos principales parsean sin corrupción.
No se detectaron leaks de secretos con las búsquedas agresivas solicitadas.
Los campos obligatorios de schema están presentes y válidos en `goals.jsonl`, `claims.jsonl` y `events.jsonl`.
La única caveat no bloqueante es de cobertura histórica: hay 239 claims pero solo 50 eventos `claim_recorded` en el stream de eventos.

## Sección 1 — Schema correctness

### Totales confirmados

| Archivo | Líneas |
|---|---:|
| `goals.jsonl` | 94 |
| `claims.jsonl` | 239 |
| `events.jsonl` | 352 |
| **Total** | **685** |

### `events.jsonl` — distribución de `event_type`

| event_type | Count |
|---|---:|
| `action_proposed` | 151 |
| `action_executed` | 85 |
| `action_failed` | 66 |
| `claim_recorded` | 50 |

### `claims.jsonl` — distribución de `claim_type`

| claim_type | Count |
|---|---:|
| `fact` | 239 |

### Violaciones de schema

| Check | Resultado |
|---|---|
| `goals.jsonl` campos obligatorios y tipos | 0 violaciones |
| `claims.jsonl` campos obligatorios y tipos | 0 violaciones |
| `events.jsonl` campos obligatorios y tipos | 0 violaciones |
| `parent_goal_id: ""` | 0 líneas |
| `events.goal_revision` ausente o null | 0 líneas |

Notas:

- Todos los goals tienen `schema_version: "goal_contract.v1"`, `goal_revision` entero `>= 1`, listas obligatorias como listas, y `parent_goal_id` como `null` o string no vacío.
- Todos los claims tienen `schema_version: "evidence_ledger.v1"` y valores válidos para `claim_type` / `verification_status`.
- Todos los events tienen `schema_version: "action_event.v1"`, `event_id`, `event_type`, `goal_id`, `goal_revision`, `session_id`, `actor` y `timestamp`.

## Sección 2 — Redaction integrity

Búsqueda agresiva sobre `~/.claw/telemetry/*.jsonl`:

| Patrón | Resultado |
|---|---|
| `sk-ant-[a-zA-Z0-9_-]{20,}` | 0 matches |
| `sk-proj-[a-zA-Z0-9_-]{20,}` | 0 matches |
| `ghp_[a-zA-Z0-9]{30,}` | 0 matches |
| `github_pat_[a-zA-Z0-9_]{60,}` | 0 matches |
| `AIza[a-zA-Z0-9_-]{30,}` | 0 matches |
| `Bearer [a-zA-Z0-9_-]{20,}` | 0 matches |
| `\b[0-9]{8,12}:[A-Za-z0-9_-]{30,}\b` | 0 matches |
| `"(api_key\|password\|secret\|token\|cookie\|credential)"\s*:\s*"[^<]` | 0 matches |

Resultado: no se detectaron secretos sin redactar.

## Sección 3 — File integrity

| Archivo | Válidas | Corruptas |
|---|---:|---:|
| `claims.jsonl` | 239 | 0 |
| `events.jsonl` | 352 | 0 |
| `goals.jsonl` | 94 | 0 |

Resultado: 0 líneas corruptas. No hay señal de bug crítico de `flock` / concurrencia en esta muestra.

## Bugs detectados (NO arreglar)

| # | Severidad | Descripción | Archivo |
|---|-----------|-------------|---------|
| 1 | Baja | Cobertura histórica incompleta del event stream: hay 239 claims en `claims.jsonl`, pero solo 50 eventos `claim_recorded` en `events.jsonl`. No rompe schema ni integridad; para PR #1.5 conviene que el Critic lea `claims.jsonl` como fuente primaria y no asuma paridad perfecta en el histórico. | `~/.claw/telemetry/events.jsonl` |

## Recomendación: B

GO con caveat. Schema, redaction e integridad JSONL están correctos, sin leaks ni corrupción, así que no hay motivo para STOP.
La caveat de `claim_recorded` histórico no bloquea arrancar PR #1.5 si el diseño consume el Evidence Ledger (`claims.jsonl`) directamente y trata el event stream como trayectoria operativa, no como índice completo retroactivo de claims.
