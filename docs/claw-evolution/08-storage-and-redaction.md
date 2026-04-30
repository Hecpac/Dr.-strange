# P0/P5 — Storage, Redaction, and Schema Versioning

## Propósito

Definir dónde vive la telemetría durable del Claw Evolution Plan y qué nunca debe
persistirse sin redacción. Este documento cubre la base de P0 y las extensiones de
confianza/duda de P5.

## Rutas

| Artefacto | Ruta |
|-----------|------|
| Goal Contract | `~/.claw/telemetry/goals.jsonl` |
| Evidence Ledger | `~/.claw/telemetry/claims.jsonl` |
| Typed Action Events | `~/.claw/telemetry/events.jsonl` |
| GDI snapshots | `~/.claw/telemetry/gdi.jsonl` |
| Critic decisions | `~/.claw/telemetry/critic_decisions.jsonl` |
| Recall requests/results | `~/.claw/telemetry/recall.jsonl` |
| FaR assessments | `~/.claw/telemetry/far.jsonl` |
| Aggregated reports | `~/.claw/telemetry/reports/` |

## Redaction obligatoria

Antes de `observe.emit`, append a JSONL o escritura a memoria durable, redactar:

- Campos con nombres que contengan `token`, `key`, `secret`, `password`, `cookie`,
  `credential`, `authorization`.
- Valores que parezcan API keys, bearer tokens, private keys, session cookies o
  approval tokens.
- PII innecesaria para la tarea.
- Raw outputs largos; guardar hash y resumen breve.
- Chain-of-thought privado.

## Formato de redaction

```json
{
  "api_key": "<REDACTED:secret>",
  "raw_output_hash": "sha256:abc...",
  "raw_output_preview": "primeros 240 chars redactados"
}
```

## Schema versioning

Todo artefacto persistido debe incluir `schema_version`. Reglas:

- Cambios compatibles agregan campos opcionales y conservan el sufijo `.v1`.
- Cambios incompatibles incrementan versión (`goal_contract.v2`).
- Lectores deben ignorar campos desconocidos.
- Migraciones deben escribirse como artefactos nuevos; no reescribir JSONL viejo.

## Retención

- JSONL es append-only.
- Rotación diaria permitida para `events.jsonl` si crece demasiado.
- Compresión opcional post-72h.
- Solo summaries revisados pasan a `MEMORY.md`; logs crudos no.

## Integridad

- Cada línea debe ser JSON válido.
- Cada evento debe incluir `schema_version`, id estable y timestamp ISO-8601.
- Writes deben ser atómicos a nivel línea y tolerantes a concurrencia.
- Si una línea corrupta aparece, el lector la reporta y continúa con las demás.

## Relación con P6

El item E de `09-openclaw-derived-adoptions.md` se implementa sobre esta capa:
token rotation nunca debe ecoar secretos a observers compartidos ni logs.

