# Postmortem accionable — Telegram 24-may

## P0 — Saneamiento de contexto

**Problema:** el brain recicló una imagen vieja que la API ya había reportado como no procesable.  
**Cambio:** antes de cada retry, si el error contiene `image in the conversation could not be processed`, eliminar todas las partes de imagen del historial que se enviará al modelo.

**Criterio de aceptación:**
- Un error de imagen rota no puede repetirse en el siguiente tool-round con el mismo asset.
- El retry debe conservar texto, tool outputs y estado relevante.
- Máximo 1 retry automático por este motivo.

## P0 — Watchdog Chrome/CDP

**Problema:** `ManagedChrome failed to start — CDP not ready on port 9250` por PIDs huérfanos/perfil ocupado.  
**Cambio:** si CDP no responde en 5 segundos, matar procesos asociados al puerto/perfil y relanzar.

**Criterio de aceptación:**
- No continuar con `proceeding anyway` si CDP es requisito del turno.
- Loguear PIDs limpiados y resultado del relanzamiento.

## P1 — Realtime TTS fallback

**Problema:** `beta_api_shape_disabled` todo el día, con degradación silenciosa.  
**Cambio:** circuito de corte: primer error 4000/beta shape disabled desactiva realtime por 24h y usa batch.

**Criterio de aceptación:**
- El usuario no queda bloqueado por voice notes.
- El log muestra que la ruta batch fue elegida intencionalmente.

## P1 — Gate Tier-3

**Problema:** over-escalation al pedir 3 decisiones cuando el usuario ya abrió sesión.  
**Cambio:** no pedir confirmaciones adicionales cuando:
- sesión/browser ya está listo,
- target account no es ambiguo,
- la acción no es irreversible o el usuario ya dio copy claro.

**Criterio de aceptación:**
- Para "configura bio en sesión ya abierta", el agente inspecciona estado y propone/ejecuta el siguiente paso sin pedir 3 decisiones.
- Para publicación irreversible sin copy, sí pide confirmación.

## P2 — Español neutral latam

**Problema:** slip de voseo: "Tenés".  
**Cambio:** filtro de salida para reemplazar voseo común y test de regresión.

**Criterio de aceptación:**
- Respuestas finales no contienen `tenés`, `querés`, `podés`, `sos`, `hacé`, `decime`.

## P2 — Research lane bloqueada

**Problema:** ToolSearch/Agent/WebSearch/WebFetch bloqueados por policy.  
**Cambio:** si la tarea requiere research externo y está bloqueado, marcar explícitamente `external_research_unavailable=true`, usar fallback conservador y no presentar el resultado como actualizado.

**Criterio de aceptación:**
- El usuario ve claramente si el resultado viene de knowledge interno y no de research actual.
