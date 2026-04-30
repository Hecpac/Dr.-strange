# P2 / P4 — Critic Protocol

## Fases

- **P2 — Critic Checkpoints v0 (in-process):** mismo proceso, formato estricto.
- **P4 — Async external critic (Tier-2/Tier-3):** segundo proceso `claude` independiente,
  sin acceso a chain-of-thought privado, mismo contrato de salida.

## Entrada al Critic

```json
{
  "goal_contract": { ... },
  "evidence_ledger_subset": [ ... claims relevantes ],
  "proposed_next_action": {
    "tool": "string",
    "args_redacted": "object",
    "tier": "tier_1 | tier_2 | tier_2_5 | tier_3",
    "rationale_brief": "string ≤ 240 chars"
  },
  "risk_level": "low | medium | high | critical",
  "gdi_snapshot": { ... },
  "recall_results": [ ... opcional ]
}
```

El crítico **no** recibe:

- chain-of-thought privado del actor,
- contenido bruto sin redactar,
- conversación completa del usuario,
- memoria irrelevante.

## Salida (formato estricto)

```json
{
  "schema_version": "critic_decision.v1",
  "decision_id": "d_<ulid>",
  "goal_id": "g_<ulid>",
  "decision": "approve | revise | block | ask_human",
  "reason_summary": "string ≤ 480 chars",
  "goal_alignment": 0.0,
  "required_fix": ["string", "..."],
  "risk_assessment": {
    "level": "low | medium | high | critical",
    "factors": ["string", "..."]
  },
  "evidence_gaps": ["claim_id no verificada", "..."],
  "decided_at": "ISO-8601"
}
```

## Reglas del crítico

- No emite ensayo libre — solo el contrato de arriba.
- `decision` toma uno de cuatro valores. **`ask_human` es válido** y se usa cuando
  la información disponible no permite una decisión segura.
- `goal_alignment` (0.0–1.0) es la lectura del Critic sobre qué tan bien la acción
  propuesta encaja con el Goal Contract. **No es lo mismo que `gdi_score`** —
  el GDI mide drift en la trayectoria; `goal_alignment` evalúa la acción puntual.
- `tier_2_5` debe evaluarse como caso sensible de Tier 2: el Critic verifica request
  explícito, rama no protegida, ausencia de force-push y plan de verificación posterior.
- `revise` requiere `required_fix` con al menos un ítem accionable.
- `block` requiere razón concreta: contradicción explícita, evidencia faltante crítica,
  o riesgo inaceptable.
- `ask_human` requiere indicar exactamente qué necesita confirmar el humano.
- Si `evidence_gaps` no está vacío y la acción es Tier-2/Tier-3, la decisión por
  defecto es `revise` o `ask_human`, no `approve`.

## Cuándo se invoca

- En P2: para todas las acciones Tier-2/Tier-3 dentro del mismo proceso.
- En P2: cuando el GDI cae en banda `critic_required`.
- En P2: cuando se cumplen ciertos `stop_conditions` reversibles.
- En P4: igual que P2, pero el Critic corre como proceso separado y se llama vía
  IPC (socket, archivo o pipe). Latencia esperada 1–5s.

## Diferencias clave entre P2 y P4

| Aspecto | P2 (in-process) | P4 (external) |
|---------|-----------------|---------------|
| Proceso | mismo `claude` | segundo `claude` independiente |
| Latencia | <1s | 1–5s |
| Sesgo compartido | sí | reducido |
| Privacidad de CoT | misma sesión | sin acceso |
| Costo | bajo | doble inferencia |
| Cuando usar | siempre como sanity-check | cuando el riesgo justifique |

## Persistencia

- Archivo: `~/.claw/telemetry/critic_decisions.jsonl` (append-only, redacted).
- Cada decisión emite también un `critic_decision_received` event.
