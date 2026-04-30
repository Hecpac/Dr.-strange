# P3 — Active Recall / Reflexion Memory

## Propósito

Recuperar memoria episódica relevante antes de acciones medium/high risk y consolidar
aprendizajes solo cuando exista evidencia suficiente de que la lección es útil,
generalizable y no contradice el Goal Contract activo.

Active Recall no reemplaza el Evidence Ledger. Recall propone contexto; el ledger
registra claims y evidencia verificable.

## Entrada

```json
{
  "schema_version": "recall_request.v1",
  "request_id": "r_<ulid>",
  "goal_id": "g_<ulid>",
  "session_id": "tg-<chat_id> | web-<id> | cron-<id>",
  "query": "string breve derivado del Goal Contract y proposed_next_action",
  "risk_level": "low | medium | high | critical",
  "action_tier": "tier_1 | tier_2 | tier_2_5 | tier_3",
  "requested_at": "ISO-8601"
}
```

## Salida

```json
{
  "schema_version": "recall_result.v1",
  "request_id": "r_<ulid>",
  "goal_id": "g_<ulid>",
  "hits": [
    {
      "memory_id": "m_<ulid>",
      "summary": "string <= 240 chars",
      "relevance": 0.0,
      "source": "MEMORY.md | memory/YYYY-MM-DD.md | task_ledger | claim",
      "evidence_refs": ["c_<ulid>", "e_<ulid>"],
      "staleness": "fresh | usable | stale"
    }
  ],
  "quality_gate": {
    "passed": true,
    "reason": "string <= 240 chars"
  },
  "recorded_at": "ISO-8601"
}
```

## Cuándo se invoca

- Antes de acciones Tier-2.5 o Tier-3.
- Cuando GDI entra en banda `caution` o `critic_required`.
- Cuando el Critic devuelve `revise` por patrones repetidos.
- Cuando una tarea se reanuda después de interrupción.

## Quality gate para consolidar memoria

Una reflexión solo se puede escribir a memoria durable si cumple todo:

- Está ligada a un `goal_id`, `task_id` o `claim_id`.
- Tiene outcome verificable (`passed`, `failed`, `blocked`) y evidencia.
- Es generalizable a una clase de tareas, no solo una anécdota.
- No contiene secretos, PII, raw prompts largos ni chain-of-thought privado.
- No contradice claims `verified` recientes.

## Reglas

- Recall puede aumentar cautela, pero no puede aprobar acciones sensibles por sí solo.
- Hits `stale` se muestran como contexto débil, nunca como evidencia fuerte.
- Si no hay hits relevantes, registrar resultado vacío; no inventar memoria.
- Solo lecciones revisadas se destilan hacia `MEMORY.md`.
- La memoria diaria (`memory/YYYY-MM-DD.md`) puede guardar notas operativas, pero no
  reemplaza el ledger append-only.

## Persistencia

- Requests/results: `~/.claw/telemetry/recall.jsonl` (append-only, redacted).
- Memoria durable revisada: `MEMORY.md`.
- Notas de trabajo: `memory/YYYY-MM-DD.md`.

