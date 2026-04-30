# P5 — FaR Doubt Flags + Tool-Grounded Confidence

## Propósito

Hacer que la confianza reportada dependa de evidencia real y de dudas
estructuradas. El sistema debe expresar incertidumbre explícita cuando una claim
carece de respaldo, depende de inferencia o contradice señales previas.

FaR aquí significa Fact-and-Reflection: separar hechos verificados, inferencias y
reflexiones operativas para reducir sobreconfianza.

## Doubt flags

| Flag | Cuándo aparece |
|------|----------------|
| `missing_tool_evidence` | Claim factual sin evidencia de tool/file/API |
| `stale_evidence` | Evidencia vieja para una claim temporalmente inestable |
| `conflicting_claims` | Claim contradice una claim `verified` previa |
| `assumption_required` | La decisión requiere una asunción no confirmada |
| `external_state_unknown` | Estado externo no fue verificado con herramienta |
| `low_observability` | La acción no deja evidencia suficiente para verificar |
| `user_confirmation_needed` | Falta autorización o preferencia humana |

## Esquema

```json
{
  "schema_version": "far_assessment.v1",
  "assessment_id": "far_<ulid>",
  "goal_id": "g_<ulid>",
  "claim_ids": ["c_<ulid>"],
  "confidence": 0.0,
  "confidence_basis": {
    "verified_claims": 0,
    "unverified_claims": 0,
    "tool_evidence_refs": 0,
    "freshness": "fresh | mixed | stale"
  },
  "doubt_flags": [
    {
      "flag": "missing_tool_evidence",
      "severity": "low | medium | high | critical",
      "reason": "string <= 240 chars",
      "required_resolution": "string <= 240 chars"
    }
  ],
  "recommended_decision": "continue | revise | ask_human | block",
  "assessed_at": "ISO-8601"
}
```

## Reglas de confianza

- `confidence` no se calcula desde tono del modelo; se calcula desde evidencia.
- Claims `fact` con `verification_status != verified` reducen confianza.
- Claims `assumption` pueden ser útiles, pero no elevan confianza factual.
- Si hay `conflicting_claims` severity high/critical, recomendar `revise` o `block`.
- Si hay `user_confirmation_needed`, recomendar `ask_human`.
- Para Tier-2.5/Tier-3, `confidence` alta requiere evidencia fresca.

## Cómo se usa en respuestas

- Reportar hechos con evidencia como hechos.
- Reportar inferencias como inferencias.
- Cuando falte evidencia, decir qué falta y cuál es el próximo paso verificable.
- Evitar frases absolutas si hay flags medium/high.

## Persistencia

- Archivo: `~/.claw/telemetry/far.jsonl` (append-only, redacted).
- Cada evaluación relevante puede emitir un evento `claim_recorded` o
  `risk_escalated` si descubre una brecha crítica.

