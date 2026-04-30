# P1 — Goal Drift Index (GDI-lite)

## Propósito

Métrica continua que mide cuánto se está desviando la ejecución actual respecto al
**Goal Contract activo**. En P1 corre en modo **log-only** durante una fase de
calibración — no bloquea, solo registra. En P1b se promueve a calibrated gating
para acciones Tier-2/Tier-3.

## Anchor

El anchor del drift monitor es el **Goal Contract**, no el primer mensaje del usuario
ni el último turno. Cada `gdi_snapshot` se calcula contra el `goal_id` activo.

## Señales de entrada

NO observar "thoughts" como señal principal. Combinar señales de los `typed_action_events`:

- distancia entre `proposed_next_action.tool` y `goal_contract.allowed_actions`
- presencia de `proposed_next_action.tool` en `goal_contract.disallowed_actions`
- contradicción con `constraints`
- número de `action_failed` consecutivos
- claims `inference` o `unverified` por encima de threshold
- escaladas de riesgo (`risk_escalated`)
- contradicción con claims `verified` previas
- file edits fuera de WORKSPACE_ROOT esperado
- tool calls a sistemas no incluidos en el plan inicial
- pérdida de cobertura de `success_criteria`

## Salida del snapshot

```json
{
  "schema_version": "gdi_snapshot.v1",
  "snapshot_id": "gdi_<ulid>",
  "goal_id": "g_<ulid>",
  "session_id": "...",
  "gdi_score": 0.0,
  "band": "continue | caution | critic_required | stop",
  "signals": [
    {"name": "tool_in_disallowed", "value": false, "weight": 0.0},
    {"name": "constraint_contradiction", "value": "...", "weight": 0.3},
    {"name": "consecutive_failures", "value": 1, "weight": 0.05}
  ],
  "reason_summary": "string ≤ 240 chars",
  "computed_at": "ISO-8601"
}
```

## Bandas iniciales (sujetas a calibración)

| Score | Banda | Acción en P1 (log-only) | Acción en P1b (calibrated gating) |
|-------|-------|-------------------------|-----------------------------------|
| 0.00–0.24 | `continue` | log | continúa |
| 0.25–0.49 | `caution` | log + flag | log + flag, considera Active Recall |
| 0.50–0.74 | `critic_required` | log | dispara `critic_review_requested` antes de Tier-2/Tier-3 |
| 0.75–1.00 | `stop` | log | bloquea acción y pide al usuario |

**Importante:** estos cortes son punto de partida. P1 sirve para recopilar
distribuciones reales y ajustar antes de promover a gating.

## Reglas

- En P1 el GDI **no bloquea**. Solo emite `gdi_snapshot` events.
- En P1b el gating efectivo combina `gdi_score` + `risk_level` + tipo de acción.
  No se usa un threshold fijo único (ej. 0.15) como hard gate.
- `gdi_score` no es la misma cosa que el `goal_alignment` que emite el Critic
  (ver `05-critic-protocol.md`). Mantener nombres distintos para evitar colisión.
- `signals` debe ser auditable: cada señal nombrada, su peso, y su valor en el snapshot.
- El score se calcula en cada `action_proposed`, en cada `risk_escalated` y opcionalmente
  cada N segundos.
- No se promedia históricamente — cada snapshot es puntual contra el estado actual.

## Persistencia

- Archivo: `~/.claw/telemetry/gdi.jsonl` (append-only, redacted).
- Reportes agregados (P1 → P1b) viven en `~/.claw/telemetry/reports/`.

## Calibración (P1 → P1b)

Después de N días de P1 log-only:

1. Análisis de la distribución de `gdi_score` por banda y por tier.
2. Identificación de falsos positivos y falsos negativos contra outcomes reales.
3. Ajuste de pesos por señal y de los cortes de banda.
4. Promoción a P1b solo cuando los cortes ofrezcan precisión razonable
   en datos reales (no en intuición).
