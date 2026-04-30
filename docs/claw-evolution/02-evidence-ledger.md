# P0 — Evidence Ledger

## Propósito

Registro de claims relevantes y su respaldo. Cada afirmación importante que Claw
emite o usa para decidir debe estar **vinculada a tool evidence** o **marcada como
inferencia / asunción**.

## Esquema

```json
{
  "schema_version": "evidence_ledger.v1",
  "claim_id": "c_<ulid>",
  "goal_id": "g_<ulid>",
  "claim_text": "string — la afirmación, en lenguaje natural breve",
  "claim_type": "fact | inference | assumption | decision | risk_signal",
  "evidence_refs": [
    {
      "kind": "tool_call | file_read | log_line | memory_entry | user_message | external_api",
      "ref": "id, path, url, hash, o snippet redactado",
      "captured_at": "ISO-8601"
    }
  ],
  "verification_status": "verified | unverified | contradicted | stale",
  "confidence": 0.0,
  "depends_on": ["c_<ulid>", "..."],
  "created_at": "ISO-8601"
}
```

## Reglas

- Cada claim importante debe tener al menos una `evidence_refs` o ser etiquetada
  explícitamente como `inference` / `assumption`.
- Una claim `verified` requiere `evidence_refs` con `kind in {tool_call, file_read, external_api}`
  y `captured_at` reciente.
- Una claim `inference` no tiene evidencia directa pero declara su origen
  (ej. "derivado de claim c_X y c_Y").
- Una claim `contradicted` se mantiene en el ledger pero queda marcada — no
  desaparece. La contradicción produce un evento `risk_signal`.
- `confidence` es opcional en P0; se vuelve obligatoria en P5 (FaR).
- Las claims son append-only. Para rectificar, se emite una claim nueva
  con `claim_type: decision` que referencia la claim previa.

## Lo que NO debe pasar

- Claims sueltas sin trazabilidad.
- Conclusiones sin origen.
- "Parece que" disfrazado de hecho.
- Estado de verificación implícito.
- Sobre-escribir claims pasadas.

## Persistencia

- Archivo: `~/.claw/telemetry/claims.jsonl` (append-only, redacted).
- Cada claim se emite también como evento `claim_recorded` en el event stream.

## Ejemplo

```json
{
  "schema_version": "evidence_ledger.v1",
  "claim_id": "c_01HYYYYYYYYY",
  "goal_id": "g_01HXXXXXXXXX",
  "claim_text": "El daemon com.pachano.claw está corriendo (PID 32786, port 8765).",
  "claim_type": "fact",
  "evidence_refs": [
    {
      "kind": "tool_call",
      "ref": "launchctl list com.pachano.claw → PID=32786",
      "captured_at": "2026-04-28T14:01:12Z"
    },
    {
      "kind": "tool_call",
      "ref": "lsof -nP -iTCP:8765 -sTCP:LISTEN → 127.0.0.1:8765",
      "captured_at": "2026-04-28T14:01:13Z"
    }
  ],
  "verification_status": "verified",
  "confidence": 0.99,
  "depends_on": [],
  "created_at": "2026-04-28T14:01:14Z"
}
```
