# P0 — Typed Action Events

## Propósito

Cada acción relevante del sistema se emite como un evento tipado, append-only y
machine-parseable. Es la fuente primaria de señal para GDI, Critic y Recall.
**Reemplaza el uso de "thoughts" como señal observable** — solo observamos acciones,
tool calls, file edits, claims y escaladas de riesgo.

## Tipos de eventos

| Tipo | Cuándo |
|------|--------|
| `goal_initialized` | Se crea un Goal Contract |
| `goal_updated` | El contrato cambia (objetivo, constraints, etc.) |
| `claim_recorded` | Se añade entrada al Evidence Ledger |
| `evidence_linked` | Se vincula evidencia nueva a una claim existente |
| `action_proposed` | Antes de ejecutar; el actor propone next_action |
| `action_executed` | Después de ejecutar |
| `action_failed` | Tool call falla, build error, etc. |
| `risk_escalated` | Tier sube; se detecta condición sensible |
| `critic_review_requested` | Se pide revisión del crítico |
| `critic_decision_received` | Llega veredicto del crítico |
| `stop_condition_triggered` | Se cumple un stop condition del Goal Contract |
| `recall_requested` | Active Recall consulta memoria episódica |
| `recall_result_recorded` | Llegan los hits |
| `gdi_snapshot` | Lectura puntual del GDI (P1+) |

## Esquema base

```json
{
  "schema_version": "action_event.v1",
  "event_id": "e_<ulid>",
  "event_type": "string (uno de la tabla)",
  "actor": "claw | critic | user | scheduler | external",
  "goal_id": "g_<ulid>",
  "session_id": "tg-<chat_id> | web-<id> | cron-<id>",
  "proposed_next_action": {
    "tool": "string",
    "args_redacted": "object",
    "tier": "tier_1 | tier_2 | tier_2_5 | tier_3",
    "rationale_brief": "string ≤ 240 chars"
  },
  "risk_level": "low | medium | high | critical",
  "claims": ["c_<ulid>", "..."],
  "evidence_refs": ["..."],
  "result": {
    "status": "success | failure | pending | skipped | blocked",
    "output_hash": "sha256: ... (no raw output)",
    "error": "string | null"
  },
  "timestamp": "ISO-8601"
}
```

## Reglas

- Append-only. Nunca se editan eventos pasados.
- `args_redacted` aplica reglas de redaction: tokens, secrets, PII, contenido bruto largo.
  Se reemplazan por hashes o placeholders (`<REDACTED:token>`, `<SHA256:...>`).
- `rationale_brief` no es chain-of-thought — es una etiqueta corta del por qué de la
  acción ("subir cambios pendientes a origin/main", "verificar puerto Blender MCP").
- Cada evento referencia el `goal_id` activo al momento.
- Eventos de tipo `risk_escalated` o `stop_condition_triggered` activan automáticamente
  un `critic_review_requested` en P2+.

## Persistencia

- Archivo: `~/.claw/telemetry/events.jsonl` (append-only, redacted).
- Rotación diaria con compresión opcional (gzip post-72h).

## Ejemplo

```json
{
  "schema_version": "action_event.v1",
  "event_id": "e_01HZZZZZZZZZ",
  "event_type": "action_executed",
  "actor": "claw",
  "goal_id": "g_01HXXXXXXXXX",
  "session_id": "tg-574707975",
  "proposed_next_action": {
    "tool": "git_push",
    "args_redacted": {"remote": "origin", "branch": "main"},
    "tier": "tier_2_5",
    "rationale_brief": "publish 4 local commits to origin/main; user authorized."
  },
  "risk_level": "medium",
  "claims": ["c_01HYYYYYYYYY"],
  "evidence_refs": ["e_<prev>"],
  "result": {
    "status": "success",
    "output_hash": "sha256:abc...",
    "error": null
  },
  "timestamp": "2026-04-28T14:05:30Z"
}
```
