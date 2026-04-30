# P0 — Goal Contract

## Propósito

Representación explícita y machine-parseable del objetivo activo. Es el **ancla principal
del sistema** — todo Critic, todo GDI y todo Recall se evalúan contra este contrato,
no contra el primer mensaje del usuario.

## Esquema

```json
{
  "schema_version": "goal_contract.v1",
  "goal_id": "g_<ulid>",
  "objective": "string — qué se busca, en una frase",
  "constraints": ["string", "..."],
  "assumptions": ["string", "..."],
  "allowed_actions": ["read_file", "write_file", "git_commit", "send_telegram", "..."],
  "disallowed_actions": ["force_push", "deploy_production", "..."],
  "success_criteria": ["string que se pueda verificar con tool evidence", "..."],
  "stop_conditions": ["string que detenga el flujo si se cumple", "..."],
  "risk_profile": "tier_1 | tier_2 | tier_2_5 | tier_3",
  "anchor_source": "user_message_id | task_id | recall_pattern | manual",
  "parent_goal_id": "g_<ulid> | null",
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601"
}
```

## Reglas

- No se infiere el objetivo solo del contexto reciente — debe registrarse explícitamente.
- Si el objetivo cambia, se emite un evento `goal_updated` y se actualiza el contrato
  con `updated_at` nuevo. El histórico queda en JSONL.
- El contrato debe ser breve, legible y machine-parseable.
- `risk_profile` se mapea a los Tiers existentes en `SOUL.md`.
- `anchor_source` rastrea de dónde nació el goal (NO necesariamente el primer mensaje
  del usuario; puede ser una tarea heredada o un patrón reactivado por recall).
- `parent_goal_id` permite jerarquía de sub-goals.

## Persistencia

- Archivo: `~/.claw/telemetry/goals.jsonl` (append-only, redacted).
- Cada turno relevante puede emitir un evento `goal_initialized` o `goal_updated`.

## Ejemplo

```json
{
  "schema_version": "goal_contract.v1",
  "goal_id": "g_01HXXXXXXXXX",
  "objective": "Migrar TC Insurance a nuevo repo Tatiana Insurance y deployar en Vercel.",
  "constraints": [
    "no force-push a main",
    "git author debe ser Hecpac@users.noreply.github.com",
    "no tocar Vercel sin confirmación"
  ],
  "assumptions": [
    "Hector ya conectó Vercel al nuevo repo",
    "uvx está disponible en /opt/homebrew/Cellar/uv/0.9.26/bin/uvx"
  ],
  "allowed_actions": ["git_commit", "git_push", "gh_repo_create", "vercel_redeploy"],
  "disallowed_actions": ["force_push", "rm_rf", "deploy_production_without_review"],
  "success_criteria": [
    "deploy state=READY en Vercel",
    "tcinsurancetx.com responde con título 'TIC Insurance | Cotiza...'"
  ],
  "stop_conditions": [
    "build falla 3 veces seguidas",
    "Hector pide pausa"
  ],
  "risk_profile": "tier_2_5",
  "anchor_source": "user_message_id:tg-574707975:1777xxxx",
  "parent_goal_id": null,
  "created_at": "2026-04-28T14:00:00Z",
  "updated_at": "2026-04-28T14:00:00Z"
}
```
