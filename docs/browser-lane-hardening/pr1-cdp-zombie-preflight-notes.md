# PR 1 — CDP zombie preflight hardening (implementation notes)

Última revisión: 2026-07-03

## Problema

`BrowserCapability.ensure_ready()` consideraba sano un Chrome solo porque
`/json/version` respondía. Un Chrome zombie sigue respondiendo
`/json/version` pero no puede abrir pestañas
(`{'code': -32000, 'message': 'Failed to open new tab - no browser is open'}`),
así que el preflight delegado daba OK y browser_use quemaba LLM contra un
navegador inservible. Además, la ruta interactiva
(`ComputerHandler._run_browser_use_session`) no corría ningún preflight:
gastaba LLM contra el `cdp_url` congelado al boot.

## Cambio

### `claw_v2/browser_capability.py`

- El probe de salud ahora es `_probe_endpoint()` = `/json/version` (con el
  rechazo headless intacto) **+ ciclo de vida de un target desechable**:
  `PUT /json/new?about:blank` → exige `id` en la respuesta →
  `GET /json/close/<id>` en `finally` (best-effort: un fallo del close no
  vuelve unhealthy un create exitoso; la pestaña del probe nunca se deja
  atrás a propósito).
- Un create de target fallido se trata como endpoint unhealthy → mismo
  camino de self-heal que un endpoint muerto: `ManagedChrome.ensure()` →
  re-probe **completo** (version + headless + target lifecycle).
- Evento nuevo `browser_capability_probe_zombie` (endpoint + error) antes
  de decidir el respawn; los eventos existentes
  (`preflight_started/ok/failed` con `stage`) se conservan.
- Ambos probes comparten `_request_json()` (timeout corto y acotado:
  `probe_timeout=2.0s` existente; sin timeouts nuevos).
- `_read_version_response` se reemplazó por `_probe_json_version` sobre
  `_request_json` — mismo comportamiento (status ≥ 400 falla, headless
  rechazado cuando `visible=True`).

### `claw_v2/computer_handler.py`

- `_run_browser_use_session` (ruta interactiva) ahora corre el **mismo
  preflight** que la delegada, después de resolver la aprobación y antes
  de gastar LLM: `ensure_ready(port=..., profile_dir=DEFAULT_CDP_PROFILE_DIR)`;
  en fallo devuelve el mensaje humano `"No pude conectar al navegador
  (CDP): ..."` y cierra la sesión (`done`, `pending_action=None`) sin
  correr el agente. En éxito, `_set_browser_use_cdp_url(endpoint)` fija el
  endpoint probado y se marca `chrome_cdp` disponible — espejo de la
  delegada.
- El preflight corre **después** de la aprobación a propósito: una tarea
  que va a quedar `awaiting_approval` no debe enfocar/lanzar Chrome.

## Comportamiento antes / después

| caso | antes | después |
|---|---|---|
| CDP muerto (delegada) | respawn + re-probe version | igual, re-probe incluye target |
| CDP zombie (version OK, tabs no) | **pasaba como sano** | unhealthy → respawn → re-probe completo |
| CDP headless | rechazado | rechazado igual (etapa version, sin probe de target) |
| ruta interactiva | **sin preflight** (LLM contra endpoint no probado) | mismo preflight que la delegada |
| sano | version OK → ready | version + create/close tab → ready, sin respawn |

## Tests

- `tests/test_browser_capability.py` — reescrito con un fake CDP ruteado
  por URL (`/json/version`, `/json/new`, `/json/close/<id>`), sin red ni
  Chrome real. Conserva los 7 tests existentes (semántica intacta) y añade
  `BrowserCapabilityZombieTests`: zombie → respawn → re-probe OK; zombie
  persistente post-respawn → `BrowserCapabilityError`
  (stage `verify_after_start`); `/json/new` sin `id` = zombie; el tab del
  probe siempre se intenta cerrar; fallo del close no marca unhealthy.
- `tests/test_computer.py` — `InteractiveBrowserPreflightTests`: la ruta
  interactiva llama `ensure_ready` **antes** de `run_task` (orden
  verificado), usa el endpoint probado, y un preflight fallido corta sin
  gastar LLM.
- Tests existentes que alcanzan la ruta interactiva recibieron una
  capability fake (`test_computer.py`, `test_computer_gate.py`,
  `test_bot.py` ×5) — sin ella habrían tocado el Chrome real de la máquina
  al correr la suite (verificado: :9250 estaba vivo durante el desarrollo).

## Desviaciones del plan

- Ninguna funcional. El plan del recon proponía el evento zombie en el
  fail-path; quedó emitido desde `_probe_endpoint` (cubre primer probe y
  re-probe).

## Riesgos dejados intencionalmente para PR 2 / PR 3

- **PR 2 (branch aparte, PR #199)**: presupuesto LLM explícito
  (`llm_timeout`/`max_failures`).
- **PR 3 / futuro**: detección de muerte de CDP mid-task (este PR solo
  cubre pre-task); el singleton `_browser_svc` de `tools.py` sigue
  cacheando su preflight para siempre; `quick_screenshot` (sin callers en
  runtime) sigue sin preflight.
- El probe añade ~2 requests HTTP locales por preflight (crear/cerrar una
  pestaña about:blank). En un Chrome autenticado sano esto es invisible
  (la pestaña vive <1s); no hay respawn nuevo sin evidencia de CDP
  inusable (el respawn requiere create-target fallido, no un timeout
  ambiguo del close).

## Evidencia

- `tests/test_browser_capability.py`: 12 passed.
- Suites afectadas (`test_browser_capability`, `test_computer`,
  `test_computer_gate`, `test_computer_handler_scope_leak`,
  `test_browser_profiles`, `test_chrome`,
  `test_architecture_invariants`): **246 passed**.
- `tests/test_bot.py` completo: **169 passed** (3:11).
- `ruff check` limpio; `ruff format --check` limpio en los archivos
  reescritos por este PR.
- **No corrido**: suite completa (~6 min; puede reiniciar el daemon vivo
  desde este checkout) y smoke en vivo — requiere autorización explícita
  (restart de producción). Este slice NO está cerrado según la regla del
  repo hasta ese smoke.
