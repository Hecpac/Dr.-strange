# Mac Chat Transport — Design Spec

**Date:** 2026-04-08
**Branch:** feat/pending-items
**Status:** Proposed

## Problem

Hoy el agente vive principalmente en Telegram.

Eso funciona como transporte remoto, pero tiene límites claros para uso diario en Mac:

- no hay ventana local de chat
- no hay UX nativa para drag and drop, historial visible y multitarea
- approvals, traces y artefactos quedan menos accesibles que deberían
- pegar links, imágenes o revisar sesiones largas desde Telegram es fricción innecesaria

El objetivo no es rehacer el agente. El objetivo es agregar una superficie local de chat en Mac reutilizando el core actual (`BotService`, `BrainService`, `observe`, `approval`, `memory`).

## Goal

Agregar un chat local para Mac que:

- comparta el mismo core del bot actual
- soporte texto, links e imágenes
- permita usar páginas abiertas del Chrome del bot como contexto
- permita ver traces y approvals
- conviva con Telegram sin duplicar lógica
- pueda evolucionar después a una app nativa sin tirar el trabajo inicial

## Constraint

El repo hoy es Python-first.

No existe stack frontend en producción dentro del repo:

- no hay `package.json`
- no hay app web existente
- no hay app Swift/SwiftUI
- sí existe patrón de transportes (`TelegramTransport`) y un runtime central en `lifecycle.py`

Eso hace que la mejor estrategia inicial no sea una app nativa completa, sino una nueva capa de transporte encima del core existente.

## Options Considered

### 1. Local Web Chat on `localhost`

Una UI web local servida por el proceso Python del bot.

**Pros**
- menor costo de implementación
- reutiliza todo el backend actual
- fácil para links, imágenes, traces y approvals
- sirve como base para app wrapper posterior
- depuración simple desde navegador

**Cons**
- no se siente totalmente nativa
- notificaciones y atajos globales requieren una segunda capa

**Fit**
- mejor opción para MVP

### 2. Native SwiftUI App

App macOS real con ventana, menubar, notificaciones y hooks del sistema.

**Pros**
- mejor UX final
- integración real con macOS
- buena base para shortcuts, share sheet, menubar y drag-and-drop

**Cons**
- costo inicial más alto
- duplica trabajo si todavía no existe una API local estable
- testing e iteración más lentos al inicio

**Fit**
- buena opción final, mala opción para primera iteración

### 3. Menubar App

Cliente ligero en barra superior con popover.

**Pros**
- acceso rápido
- muy buena UX para prompts cortos, links y approvals
- se siente más “assistant en Mac”

**Cons**
- se queda corto para conversaciones largas
- igual necesita una API local o una capa de integración

**Fit**
- gran fase 2, no fase 1

### 4. Raycast Extension

Cliente rápido tipo command palette.

**Pros**
- alta velocidad de uso
- bueno para workflows cortos
- bajo costo relativo

**Cons**
- pobre para conversaciones largas
- peor para artefactos, imágenes, traces y debugging

**Fit**
- complemento útil, no superficie principal

### 5. Tauri / Electron Wrapper

App desktop empaquetada sobre UI web.

**Pros**
- despliegue desktop con una sola UI
- Tauri da acceso a APIs nativas sin stack Swift completo

**Cons**
- mete una segunda plataforma de runtime
- para este repo sería más trabajo que un web local simple
- Electron sería demasiado pesado para el caso actual

**Fit**
- posible después, no recomendado como primer paso

### 6. Terminal UI

Chat local en terminal.

**Pros**
- rapidísimo de construir
- casi cero riesgo técnico
- excelente para debugging y power users

**Cons**
- no resuelve el deseo de “chat en la Mac” en sentido de app/UI
- mala superficie para imágenes, artifacts y aprobaciones visuales

**Fit**
- útil como herramienta interna, no como producto final

## Recommendation

La decisión recomendada es:

### Phase 1

**Local web chat en `localhost`** servido por Python, reutilizando `BotService`.

### Phase 2

**Menubar app para macOS** consumiendo la API local del bot.

### Phase 3

Si la experiencia demuestra valor real, migrar el cliente a **SwiftUI nativo** conservando la misma API local.

Esta secuencia minimiza riesgo, evita reescribir el core y deja un camino limpio hacia UX nativa.

## Architecture Decision

La capa Mac no debe hablar directamente con `BrainService`.

Debe hablar con un endpoint local que encapsule:

- sesiones
- autorización local
- formatting de respuestas
- approvals
- traces
- multimodal

Eso mantiene Telegram, Mac chat y futuros clientes sincronizados sobre el mismo contrato.

## Proposed Architecture

```text
                +----------------------+
                |  TelegramTransport   |
                +----------+-----------+
                           |
                +----------v-----------+
                |    BotService        |
                |  Brain / Memory /    |
                |  Approval / Observe  |
                +----------+-----------+
                           |
           +---------------+----------------+
           |                                |
+----------v-----------+        +-----------v-----------+
|   Local Chat API     |        |  Existing Daemon      |
|  HTTP + SSE/stream   |        |  Cron / background    |
+----------+-----------+        +-----------------------+
           |
  +--------+--------+
  |                 |
+-v-------------+  +-v----------------+
| Local Web UI  |  | Future Menubar   |
| localhost     |  | SwiftUI client   |
+---------------+  +------------------+
```

## Core Principle

`BotService` sigue siendo la frontera de dominio.

El chat de Mac no debe duplicar:

- shortcut parsing
- session memory
- wiki/learning hooks
- approvals
- trace emission

Si el cliente necesita capacidades nuevas, se añaden al core y las consumen ambos transportes.

## Phase 1 — Local Web Chat

### Scope

Agregar una API HTTP local y una UI HTML mínima.

### Capabilities

- enviar texto
- pegar links
- enviar imagen
- listar tabs abiertas del Chrome del bot
- analizar la tab actual o una tab elegida
- sacar screenshot de la tab actual
- ver respuestas largas sin límite Telegram
- listar traces recientes
- abrir replay de una traza
- listar y aprobar acciones pendientes

### API Contract

#### `POST /api/chat`

Request:

```json
{
  "session_id": "mac-local",
  "text": "revisa este link https://..."
}
```

Response:

```json
{
  "reply": "...",
  "trace_id": "optional",
  "artifacts": []
}
```

#### `POST /api/multimodal`

Multipart o JSON con imagen + texto.

#### `GET /api/traces?limit=20`

Lista de traces recientes.

#### `GET /api/traces/{trace_id}`

Replay completo de eventos.

#### `GET /api/approvals`

Pendientes.

#### `POST /api/approvals/{approval_id}/approve`

Aprueba usando token.

#### `POST /api/approvals/{approval_id}/reject`

Rechaza o aborta.

#### `GET /api/browser/tabs`

Lista tabs abiertas del Chrome gestionado por el bot.

Response:

```json
{
  "tabs": [
    {"index": 0, "title": "OpenAI Pricing", "url": "https://openai.com/pricing"}
  ]
}
```

#### `POST /api/browser/analyze`

Analiza una tab existente sin pedir URL manualmente.

Request:

```json
{
  "session_id": "mac-main",
  "page_index": 0,
  "prompt": "revisa esta página"
}
```

#### `POST /api/browser/screenshot`

Toma screenshot de la tab actual o de una tab seleccionada.

### UI Shape

La UI inicial debe ser simple:

- columna principal de chat
- caja de texto fija abajo
- botón para adjuntar imagen
- panel lateral opcional para traces y approvals
- panel opcional de `Tabs`

No hace falta React para v1. Un HTML server-rendered o una página estática con JS mínimo es suficiente.

## Browser Context Module

### Goal

Permitir que el chat local use páginas abiertas como contexto de trabajo.

Esto no significa “leer cualquier ventana del sistema”.

La integración inicial debe limitarse a **Chrome gestionado por el bot** (`ManagedChrome` + CDP), que ya existe en el runtime actual.

### Existing Base in Repo

Ya existen piezas reutilizables:

- `BotService._chrome_pages_response()` en `claw_v2/bot.py`
- `BotService._chrome_browse_response()` en `claw_v2/bot.py`
- `BotService._chrome_shot_response()` en `claw_v2/bot.py`
- `DevBrowserService.connect_to_chrome()` en `claw_v2/browser.py`
- `DevBrowserService.chrome_navigate()` en `claw_v2/browser.py`
- `DevBrowserService.chrome_screenshot()` en `claw_v2/browser.py`
- `ManagedChrome` cableado en `claw_v2/lifecycle.py`

### Phase 1 Scope

El chat de Mac debe soportar:

- ver tabs abiertas del Chrome del bot
- seleccionar una tab
- usar esa tab como contexto
- pedir “resume/revisa esta tab”
- tomar screenshot de la tab actual

### Phase 1 Non-goals

No debe intentar todavía:

- leer Safari
- leer cualquier Chrome personal del usuario fuera del perfil del bot
- inspeccionar cualquier ventana arbitraria del sistema
- hacer OCR global del escritorio como sustituto del browser context

### Suggested UX

Panel lateral `Tabs` con:

- lista de tabs (`title`, `url`, `index`)
- botón `Refresh`
- botón `Usar contexto`
- botón `Analizar`
- botón `Screenshot`

Flujo esperado:

1. El usuario abre una página con `chrome_browse` o ya la tiene en el Chrome del bot.
2. La UI lista tabs abiertas.
3. El usuario elige una tab.
4. El chat puede mandar algo como:
   - `revisa esta tab`
   - `qué dice esta landing`
   - `haz screenshot y dime qué falla`

### Implementation Shape

No hace falta meter esta lógica en el cliente.

La API local debe exponer browser context sobre las capacidades ya existentes del runtime.

La fuente de verdad sigue siendo el core Python.

### Suggested Files

| File | Purpose |
|------|---------|
| `claw_v2/chat_api.py` | HTTP handlers |
| `claw_v2/web_transport.py` | arranque del servidor local |
| `claw_v2/session_store.py` | opcional, si se quiere separar sesiones por surface |
| `claw_v2/browser_context.py` | opcional, adaptador limpio para tabs/screenshot/analyze |
| `claw_v2/static/chat.html` | UI inicial |
| `claw_v2/static/chat.js` | envío/recepción básico |
| `claw_v2/static/chat.css` | estilos mínimos |
| `claw_v2/lifecycle.py` | iniciar Telegram + web juntos o por config |

### Tech Choice

Mantenerlo en Python.

Si se quiere mínimo costo:

- `http.server` o servidor WSGI simple

Si se quiere algo más limpio:

- `FastAPI` o `Starlette`

Mi recomendación: **FastAPI**.

Razón:

- endpoints claros
- soporte natural para multipart
- fácil de servir localmente
- buena base si luego el cliente menubar consume la misma API

## Phase 2 — Menubar App

### Scope

Cliente macOS ligero que consume la API local.

### Capabilities

- abrir/cerrar desde menubar
- enviar texto rápido
- drag-and-drop de links o imágenes
- notificaciones para approvals
- botón “abrir chat completo”

### Why After Web

Porque el menubar no debería hablar directo con Python interno.

Debe consumir una API ya estabilizada.

## Phase 3 — Native SwiftUI App

### Scope

Cliente nativo completo.

### Features

- historial por sesión
- menubar + ventana completa
- share extension
- notificaciones ricas
- atajo global para abrir chat
- inspector de traces
- approvals UI

### Why Not First

Porque el mayor riesgo hoy no es de UI; es de contrato.

Primero hay que definir bien:

- cómo se envía un mensaje
- cómo se reciben artifacts
- cómo se muestran traces
- cómo se resuelven approvals

Eso se valida más rápido con web local.

## State Model

El cliente Mac no debe inventar su propia memoria.

Debe mandar `session_id` y dejar que el core use:

- `brain.memory`
- `observe`
- `approval`
- `wiki`
- `learning`

Sesiones sugeridas:

- `mac-main`
- `mac-quick`
- `mac-debug`

Luego el cliente puede mapear varias conversaciones a distintos `session_id`.

## Security Model

Como la API corre local:

- bind por defecto a `127.0.0.1`
- no exponer a red
- puerto configurable
- token local opcional para protección básica del cliente

No hay necesidad de auth compleja en v1 si solo escucha en loopback, pero sí conviene un token efímero o secreto local si luego habrá múltiples clientes.

## Config Changes

Agregar a `config.py`:

```python
web_chat_enabled: bool          # default True or False según rollout
web_chat_host: str              # default 127.0.0.1
web_chat_port: int              # default 8765
web_chat_token: str | None      # optional local auth
telegram_enabled: bool          # optional explicit toggle
```

## Lifecycle Changes

`lifecycle.py` debe iniciar transportes en paralelo:

- Telegram si está configurado
- Web chat si está habilitado

Algo así:

```text
build_runtime()
start TelegramTransport if configured
start WebTransport if enabled
start daemon loop
shutdown all in finally
```

## Testing

### `tests/test_chat_api.py`

- `POST /api/chat` llama `bot.handle_text`
- conserva `session_id`
- devuelve reply
- devuelve 400 en input inválido

### `tests/test_web_transport.py`

- arranque/parada del servidor
- bind correcto a loopback

### `tests/test_chat_api.py`

- `GET /api/browser/tabs` devuelve tabs del Chrome del bot
- `POST /api/browser/analyze` usa la tab elegida como contexto
- `POST /api/browser/screenshot` devuelve ruta o metadata del screenshot

### `tests/test_lifecycle.py`

- arranca Telegram y Web transport cuando corresponde
- degrada si uno falla

### `tests/test_bot.py`

- sin cambios semánticos fuertes; el web client debe ser otro transporte, no otra lógica

## Rollout Plan

### Step 1

Crear `chat_api.py` con `POST /api/chat`.

### Step 2

Crear UI mínima local:

- input
- transcript
- submit

### Step 3

Agregar traces y approvals.

### Step 4

Agregar soporte de imagen.

### Step 5

Evaluar si el uso diario justifica menubar app.

## Success Criteria

El diseño será correcto si:

- puedes chatear desde el navegador local sin Telegram
- links, imágenes y respuestas largas funcionan
- tabs del Chrome del bot son visibles y analizables
- approvals y traces son visibles desde esa UI
- no se duplicó lógica del core
- Telegram sigue funcionando sin cambios de comportamiento

## Decision Summary

La mejor ruta no es construir primero una app nativa completa.

La mejor ruta es:

1. **API local + web chat**
2. **menubar app consumiendo esa API**
3. **SwiftUI completo solo si el uso real lo justifica**

Eso te da chat en la Mac rápido, sin desarmar la arquitectura actual, y deja una base limpia para una experiencia nativa después.
