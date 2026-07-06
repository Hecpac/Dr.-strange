# Slice 1b — outbox durable de notificaciones: evidencia de smoke en vivo

Fecha: 2026-07-06 · PR #219 → main `2010044` · deploy clone `~/srv/claw-daemon` pid 44843

## Contexto

Hallazgo #6 del pase de puntos ciegos 2026-07-06: la notificación de un task
terminal era fire-once best-effort — sets de dedup in-memory que mueren en el
boot, y el "future drain pass" prometido en el docstring de
`finalize_terminal_notification` no existía para `succeeded`. Un outage de
Telegram en la ventana de entrega perdía el aviso para siempre.

Slice 1b (spec congelado en entrevista 2026-07-06): el callback de fallo
encola una fila durable `agent_jobs` kind `owner_notification` (inline
primero, encolar solo en fallo; tasks-only; at-least-once; cutoff 24h con
terminalización por evento — nunca delete) y `OwnerNotificationDrainRunner`
la entrega off-tick vía `stop_notifier`.

## Deploy y boot limpio

- Clone a `2010044` (merge PR #219; CodeRabbit pass sin findings, cero
  reviews pendientes, Fast gate + secret-scan verdes), `./scripts/restart.sh`
  → preflight backup `claw-20260706-205235.db`, pid 44843.
- **12/12 `startup_healthcheck_ok`**, 0 failed, puerto 8765 LISTEN, watchdog
  `no action (ok)`.

## Smoke por la superficie real

Fila sembrada **con el helper real desplegado**
(`lifecycle.enqueue_owner_notification` desde el venv del clone contra la DB
de prod — mismo camino de código que ejecuta el callback de fallo):
`job:43c9584ada27`, `resume_key=owner_notif:smoke-1b#attempt-0`, estado
`queued`, dedup activo.

| Paso | Evidencia (event-id) |
|------|----------------------|
| Drain reclama el job (ciclo de 300s) | **578167 `job_claimed`** 20:57:42, kind `owner_notification`, status running |
| Send vía `stop_notifier` al chat del owner | sin excepción (API de Telegram aceptó) |
| Job terminaliza entregado | **578189 `job_completed`** 20:57:43, status completed |
| Evento de audit del outbox | **578193 `owner_notification_delivered`** 20:57:43, attempts=1 |
| Recepción por el owner | mensaje "🔔 Smoke Slice 1b…" recibido — **confirmado por Hector** (~21:05 UTC) |

Los carriles negativos (retry con backoff, caducidad 24h que terminaliza con
`owner_notification_expired`, payload inválido, agotamiento de intentos,
max_per_cycle) quedan **test-locked** en
`tests/test_owner_notification_outbox.py` (9 tests) — no se ejercieron en vivo
a propósito (habría requerido romper Telegram; misma decisión que el smoke del
Slice 1a).

## Residuales anotados (no defectos del slice)

1. El payload del evento `owner_notification_delivered` muestra
   `notification_key: [REDACTED]` — el redactor de observe matchea el nombre
   del campo (`*_key`). Cosmético: el `job_id` correlaciona igual; candidato a
   micro-ajuste de naming si molesta en forense.
2. Micro-ventana crash-entre-evento-y-send no cubierta (decisión explícita de
   la entrevista: costura inline-primero).

## Veredicto

Arco del slice **PASS** en producción: fila durable adeudada → drain off-tick
→ entrega real por Telegram → job `completed` + eventos de audit. La clase de
falla "aviso perdido con warning" queda cerrada: el mismo camino que hoy
entregó la fila sembrada entregará las filas que el callback encole ante un
send fallido real.
