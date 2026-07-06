# Slice 1a — /reissue de approvals: evidencia de smoke en vivo

Fecha: 2026-07-06 · PR #217 → main `f703a52` · deploy clone `~/srv/claw-daemon` pid 25228

## Contexto

Hallazgo #1 del pase de puntos ciegos 2026-07-06: el token crudo de un approval
solo vive en el envío efímero de Telegram (hash-only en disco, AH1, retry=1);
un send fallido dejaba el Tier 3 bloqueado en silencio hasta expirar a los
900s. Slice 1a agrega `/reissue <id>` (interrupt command owner-only) que rota
el hash atómicamente y reinicia la ventana TTL — pending-only, expired terminal.
Spec congelado en entrevista pre-slice 2026-07-06.

## Deploy y boot limpio

- Clone actualizado a `f703a52` (merge PR #217), `./scripts/restart.sh` →
  preflight backup verificado `claw-20260706-191657.db`, pid 25228.
- Señal positiva: **12/12 `startup_healthcheck_ok`** en observe_stream
  (ids 573888–573899), puerto 8765 LISTEN, watchdog `no action (ok)`.
- Señal negativa: stderr sin traceback ni RuntimeDatabaseError (archivo
  congelado — instancia sin tareas browser; ver hallazgo "stderr mudo" del
  mismo pase: la señal positiva de boot es observe_stream, no stderr).

## Smoke por la superficie real (Telegram, sesión del owner)

Registros inertes (`action=smoke_slice1a`) sembrados vía ApprovalManager
file-backed (mismo root `~/.claw/pending_approvals`, mismo `APPROVAL_SECRET`
del daemon). Nota de alcance: la *creación* de approvals no fue tocada por el
slice; lo probado en vivo es todo lo que el slice cambió.

### Ronda 1 — el token viejo muere tras /reissue (id `945d590b5da9814c`)

| Paso | Evidencia |
|------|-----------|
| `/reissue 945d590b5da9814c` (turn 20:02:01, user_text_length=25) | evento **575917 approval_reissued**; reply 197 chars (mensaje 🔁) |
| TTL reiniciado | record: `first_created_at` 19:58:11 → `created_at` 20:02:01, `reissue_count=1` |
| `/approve <id> <tokenA viejo>` (20:02:20, length=42 — bytes exactos) | evento **575962 approval_rejected**, reply 17 chars ("approval rejected"); record `rejected` by human, sin `resolution_sig` |

### Ronda 2 — el token re-emitido aprueba (id `452c6a13f19edde8`)

| Paso | Evidencia |
|------|-----------|
| `/reissue 452c6a13f19edde8` | evento **576096 approval_reissued**; `first_created_at` 20:04:40 → `created_at` 20:05:14, `reissue_count=1` |
| `/approve <id> <token nuevo del 🔁>` | evento **576158 approval_approved**; record `approved` by human **con `resolution_sig` válida** (firmada por el manager del daemon) |

### Intento fallido intermedio (documentado, no defecto)

`dfa3a8f98b26d7d4`: el `/approve` de ronda 2 midió 47 chars (42 esperados) —
~5 caracteres extra pegados al token en el copy → digest mismatch → rejected.
Error de copia del operador; la ronda se repitió con registro fresco y pasó.

## Intentos de disparador orgánico (hallazgos laterales, anotados)

1. «borra el archivo X» → el brain lo movió a quarantine
   (`data/quarantine/smoke-reissue-A.txt.deleted-20260706`) por su disciplina
   quarantine-no-rm → **el gate de borrado nunca disparó** (efecto cumplido por
   ruta no gateada; candidato a revisión de diseño).
2. `file.delete` es **entrada muerta**: existe en `tool_policies.json` pero no
   hay tool registrada con ese nombre (drift política↔registry).
3. `/computer abre la Calculadora` → secuestrado por el shortcut de browser
   (`delegated_browser_task_started` → `computer_browser_use_missing_domain_grant`
   → task failed 19:48:20) — recurrencia del residual conocido del hardening
   browser 2026-07-03.
4. Con esos tres caminos cerrados se optó por registros sembrados inertes.

## Veredicto

Arco completo del slice **PASS** en producción: `/reissue` por la superficie
real → rotación atómica + TTL restart en records reales → mensaje 🔁 entregado
por Telegram → token viejo inválido → token nuevo aprueba con firma. Eventos
`approval_reissued`/`approval_rejected`/`approval_approved` en el audit trail
(el manager del daemon emite a observe_stream — verificado en vivo).

Residual UX (para Slice 1b o micro-fix): `_format_approval_reissued` imprime
`/approve` genérico; para approvals de computer el comando operativo es
`/action_approve` (ambos validan contra el mismo hash rotado).
