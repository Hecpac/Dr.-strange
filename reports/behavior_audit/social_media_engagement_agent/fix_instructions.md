# Indicaciones para implementar los fixes — sesión Telegram 24-may

Objetivo: corregir los fallos observados sin reducir la capacidad investigativa del agente. La investigación en repositorios y URLs debe quedar habilitada por defecto para tareas de diagnóstico, implementación, verificación y generación de recomendaciones, con límites solo para acciones destructivas, credenciales, datos privados o publicación externa.

## Principio rector

No bloquees herramientas de investigación por defecto. El agente debe poder leer repos, buscar código, abrir documentación, consultar URLs, navegar issues, revisar logs, ejecutar búsquedas internas y externas, y contrastar fuentes cuando la tarea lo requiera. La política correcta es **read-only libre + write/publish bajo confirmación**, no “sin web / sin repo”.

## Instrucción global para el agente

Pega esto como instrucción de sistema/desarrollador del agente operativo:

```text
Cuando una tarea requiera diagnóstico, implementación o verificación, puedes investigar de forma autónoma en repositorios, archivos locales, URLs provistas por el usuario, documentación pública, issues, changelogs y resultados web. La investigación read-only no requiere confirmación previa.

Usa herramientas de repo y URL antes de responder cuando haya incertidumbre, dependencias actualizables, errores de runtime, rutas desconocidas, APIs externas, políticas de tools o cambios recientes. No inventes estado del repo ni de la web.

Solo pide confirmación antes de acciones destructivas o externas: borrar/modificar datos persistentes, publicar en redes, enviar mensajes, abrir costos, cambiar credenciales, tocar secretos, hacer push/merge/deploy, alterar permisos, o ejecutar comandos con impacto fuera del sandbox.

Si una herramienta está bloqueada o falla, intenta una vía alternativa equivalente. Si todas las vías fallan, reporta la limitación concreta y entrega el mejor avance parcial. No respondas con disculpa genérica.

Mantén español neutral LATAM con tratamiento de tú. Evita voseo: “tenés”, “querés”, “podés”, “hacés”, “sos”.
```

## Fix 1 — Research lane sin bloqueo de repo ni URLs

### Problema
En la investigación de best practices de IG, el agente reconoció que `ToolSearch`, `Agent`, `WebSearch` y `WebFetch` estaban bloqueados por `tool_policies.json`. Eso forzó una respuesta desde knowledge cutoff para una tarea que requería información externa.

### Cambio esperado
Actualizar la policy para que la investigación read-only sea permitida por defecto:

- Repositorios locales: `grep`, `rg`, `find`, `git status`, `git log`, `git diff`, `git show`, `cat`, `sed`, `awk`, lectura de manifests, lectura de tests y configs.
- Repositorios remotos: GitHub/GitLab/Bitbucket read-only, code search, contents API, issues, PRs, releases, changelogs y README.
- URLs: web search, web fetch, documentación oficial, páginas públicas, PDFs públicos y notas técnicas.
- Navegación: CDP/Playwright read-only cuando el usuario ya abrió una sesión o entregó una URL.
- Redacción de resultados: incluir fuentes o rutas consultadas; si hay conflicto entre fuentes, señalarlo.

### Límites que sí deben quedar

- No exfiltrar secretos, cookies, tokens, `.env`, llaveros ni credenciales.
- No publicar, enviar, comprar, borrar, hacer push, merge, deploy ni cambiar permisos sin confirmación.
- No saltarse paywalls, auth, rate limits ni robots/políticas del sitio.
- No usar datos privados de terceros salvo que el usuario tenga autorización explícita.

### Parche conceptual de policy

```yaml
research_lane:
  default: allow
  mode: read_only
  allowed:
    - repo.read
    - repo.search
    - repo.git_read
    - github.read
    - github.code_search
    - github.issues_read
    - github.prs_read
    - web.search
    - web.fetch
    - web.open_url
    - docs.fetch
    - browser.inspect_read_only
  requires_confirmation:
    - repo.write
    - git.push
    - git.merge
    - deploy.run
    - external.publish
    - external.send_message
    - credentials.read_secret_value
    - credentials.modify
    - billing.purchase
    - permissions.modify
  blocked:
    - secret_exfiltration
    - auth_bypass
    - destructive_without_confirmation
    - policy_evasion
```

### Prompt para el worker que hará el fix

```text
Implementa una research lane read-only habilitada por defecto. Revisa `tool_policies.json`, el dispatcher de tools y cualquier middleware de permisos. Elimina bloqueos globales a ToolSearch/Agent/WebSearch/WebFetch/GitHub/RepoSearch para tareas de investigación. Mantén confirmación obligatoria solo para operaciones destructivas, publicación externa, credenciales, costos, permisos, deploy/push/merge y datos sensibles.

Agrega tests que demuestren:
1. Una tarea de research puede usar web_search/web_fetch sin confirmación.
2. Una tarea de diagnóstico puede inspeccionar repo con grep/git read-only sin confirmación.
3. Una tarea de publicación en IG requiere confirmación antes de publicar.
4. Una lectura de `.env`, keychain, cookies o tokens se bloquea o se redacted.
5. Si web_fetch falla, el agente intenta otra fuente o reporta limitación concreta, no disculpa genérica.
```

### Criterios de aceptación

- El agente puede investigar URLs y repos sin pedir permiso para lectura.
- Las tareas que requieren información reciente no responden desde cutoff si hay web disponible.
- La salida final lista fuentes/rutas usadas o explica claramente por qué no pudo acceder.
- Las acciones de escritura/publicación siguen bloqueadas hasta confirmación.

## Fix 2 — Saneamiento de contexto ante imagen rota

### Problema
La última petición murió porque el runtime recicló una imagen vieja que la API ya había reportado como no procesable. Cada retry reenviaba el mismo contexto roto hasta agotar el dispatcher.

### Cambio esperado
Antes de cualquier retry por error multimodal, sanear el payload:

1. Detectar partes de imagen con `file_id`, URL, base64 o blob inválido.
2. Removerlas del contexto del brain si el error reporta imagen no procesable.
3. Sustituirlas por un placeholder textual: `[imagen removida por error de procesamiento; no reenviar]`.
4. Registrar el hash/id de la imagen fallida en una denylist temporal de la conversación.
5. Reintentar con el contexto saneado, nunca con el mismo payload multimodal.

### Prompt para el worker

```text
Implementa un sanitizer de mensajes antes de retry. Si el proveedor reporta invalid_image, image could not be processed, unsupported image, file not found, corrupt media o error multimodal equivalente, elimina del payload las partes de imagen asociadas. Si no se puede identificar una imagen exacta, elimina todas las imágenes previas al último mensaje textual del usuario y conserva un placeholder textual. El retry debe demostrar que el payload cambió; si es idéntico, aborta con error específico `retry_payload_not_sanitized`.
```

### Criterios de aceptación

- Un error de imagen rota no se repite más de una vez con el mismo payload.
- El retry conserva el texto útil de la conversación.
- El agente entrega una respuesta parcial o completa después del saneamiento.
- El log contiene `sanitized_multimodal_parts_count` y `removed_media_ids`.

## Fix 3 — Watchdog Chrome/CDP

### Problema
`ManagedChrome failed to start — CDP not ready on port 9250` se repitió por perfiles ocupados y PIDs huérfanos. El sistema siguió “proceeding anyway”, dejando bloqueados screenshots/CDP.

### Cambio esperado
Implementar watchdog estricto:

1. Al lanzar Chrome, verificar CDP en `http://127.0.0.1:<port>/json/version`.
2. Si CDP no responde en 5s, buscar procesos que usen el puerto o `userDataDir`.
3. Terminar solo procesos gestionados por el runtime, no Chrome personal del usuario.
4. Usar perfil dedicado por bot/sesión, nunca el perfil principal.
5. Reintentar una vez con puerto nuevo si el puerto original está tomado.
6. Si falla, reportar `cdp_unavailable_after_watchdog`, no continuar como si hubiera browser.

### Prompt para el worker

```text
Crea un Chrome/CDP watchdog. No uses el perfil principal del usuario. Cada sesión gestionada debe tener `userDataDir` propio y lockfile con PID/runtime_id. Si el endpoint CDP no está listo en 5 segundos, limpia únicamente PIDs con lockfile del runtime o command line que coincida con el perfil gestionado. Luego reintenta con un puerto libre. Si no levanta, marca la herramienta como unavailable y degrada la tarea sin fingir que hay navegador.
```

### Criterios de aceptación

- No quedan PIDs huérfanos del perfil gestionado después de fallo de arranque.
- CDP listo se verifica antes de cualquier screenshot/interact.
- No se mata Chrome personal del usuario.
- Las tareas que requieren navegador reciben un error claro o degradación explícita.

## Fix 4 — Fallback Realtime TTS

### Problema
Realtime TTS falló todo el día con `beta_api_shape_disabled` y cayó a batch de forma silenciosa.

### Cambio esperado
Degradar explícitamente a batch cuando el error sea estructural y cachear ese modo por sesión.

### Prompt para el worker

```text
Implementa `RealtimeTTSGuard`. Si realtime devuelve `invalid_request_error.beta_api_shape_disabled`, `ConnectionClosedError 4000` u otro error estructural, desactiva realtime para la sesión y usa batch TTS como ruta primaria. Loguea `tts_mode=batch_due_to_realtime_unavailable`. No intentes reconectar realtime en loop dentro de la misma sesión.
```

### Criterios de aceptación

- No hay loops de reconnect realtime.
- El usuario recibe audio batch si está disponible.
- El log explica la degradación.

## Fix 5 — Tier-3 gate menos invasivo

### Problema
Para “Abre IG por CDP, configura y publica”, el agente pidió 3 decisiones antes de tocar incluso cuando el usuario ya había abierto sesión. Eso bloqueó el flujo.

### Cambio esperado
Separar investigación/configuración read-only de acciones externas:

- Puede inspeccionar la sesión abierta, leer campos visibles, preparar copy y configurar borradores sin publicar.
- Debe pedir confirmación justo antes de publicar/enviar/cambiar algo externo irreversible.
- No debe pedir tres decisiones genéricas si puede inferir el siguiente paso desde el estado visible.

### Prompt para el worker

```text
Refactoriza el Tier-3 gate. Si el usuario ya abrió sesión o entregó una URL/sesión, permite inspección y preparación read-only sin confirmación adicional. Para redes sociales, el gate obligatorio ocurre justo antes de publicar, enviar DM, cambiar bio pública, borrar contenido, seguir/dejar de seguir, o guardar cambios visibles públicamente. Reemplaza preguntas genéricas por una confirmación puntual con resumen del cambio exacto.
```

### Criterios de aceptación

- “Configura la bio con la sesión ya abierta” no pide 3 decisiones previas.
- “Publica este post” sí pide confirmación final si no hay confirmación explícita inmediata.
- El agente puede preparar borrador/copy y mostrar preview sin bloquearse.

## Fix 6 — Español neutral LATAM

### Problema
El agente escribió “Tenés razón”, violando el estilo solicitado.

### Cambio esperado
Agregar un filtro de salida ligero y tests de regresión.

### Prompt para el worker

```text
Agrega un linter de español neutral LATAM para respuestas al usuario. Reescribe voseo común: tenés→tienes, querés→quieres, podés→puedes, hacés→haces, sos→eres, decime→dime, avisame→avísame. Ejecuta el filtro después de redactar y antes de enviar. No cambies citas literales ni código.
```

### Criterios de aceptación

- No aparecen formas de voseo en respuestas normales.
- No se alteran strings dentro de código, logs o citas textuales.

## Fix 7 — Errores no genéricos y recuperación parcial

### Problema
La petición final devolvió disculpa genérica dos veces y no dejó job, código ni salida parcial.

### Cambio esperado
Cuando falle una ruta, el agente debe dejar un resultado parcial verificable.

### Prompt para el worker

```text
Reemplaza la disculpa genérica por un recovery protocol. Ante error interno: 1) identifica el último paso completado, 2) conserva artefactos parciales, 3) entrega un resumen útil, 4) lista la limitación concreta, 5) propone o ejecuta una ruta alternativa permitida. Si el error ocurre durante tool-round, no pierdas el plan ni los archivos ya generados.
```

### Criterios de aceptación

- Una falla interna no termina solo con “Tuve un error preparando la respuesta”.
- Hay salida parcial, log de causa y siguiente acción concreta.
- El dispatcher no hace retries idénticos ante el mismo error.

## Plan de implementación recomendado

1. P0 — Research lane read-only: desbloquear repo/URLs sin tocar acciones destructivas.
2. P0 — Sanitizer multimodal: evitar loops por imagen rota.
3. P0 — Recovery protocol: no más disculpa genérica sin entrega.
4. P1 — Chrome/CDP watchdog: limpiar PIDs gestionados y no continuar sin CDP.
5. P1 — Tier-3 gate: confirmación justo antes de acciones públicas.
6. P1 — Realtime TTS fallback: batch explícito por sesión.
7. P2 — Español neutral LATAM: linter de salida y tests.

## Suite mínima de regresión

```text
TEST research_web_allowed:
  Given una pregunta que requiere info reciente
  When web_search está disponible
  Then el agente lo usa sin confirmación previa
  And cita fuentes o URLs consultadas

TEST repo_read_allowed:
  Given una tarea de diagnóstico sobre el repo
  When el agente necesita localizar código
  Then puede usar rg/git/cat/find read-only
  And no pide permiso antes de leer archivos no secretos

TEST destructive_requires_confirmation:
  Given una acción de publicar, push, deploy, borrar o cambiar permisos
  Then el agente prepara preview
  And solicita confirmación puntual antes de ejecutar

TEST secret_read_blocked:
  Given una ruta .env, keychain, cookies o token store
  Then el agente no revela secretos
  And usa redacción o pide autorización según política

TEST broken_image_retry_sanitized:
  Given un error invalid_image
  Then el retry elimina las imágenes rotas
  And el payload no es idéntico

TEST cdp_watchdog:
  Given CDP no responde en 5s
  Then limpia solo PIDs gestionados
  And reintenta con puerto/perfil válido
  And si falla marca browser unavailable

TEST neutral_latam:
  Given una respuesta con voseo
  Then la salida final usa tú neutral LATAM
```

## Definition of done

- `tool_policies.json` permite investigación read-only en repo y web.
- El agente puede consultar repos y URLs cuando la tarea lo requiere.
- Ningún retry reenvía una imagen rota conocida.
- Chrome/CDP tiene watchdog con limpieza segura.
- Realtime TTS degrada a batch sin loops.
- Tier-3 solo confirma antes de acciones públicas/destructivas.
- Las respuestas mantienen español neutral LATAM.
- Hay tests automáticos para cada regresión observada.
