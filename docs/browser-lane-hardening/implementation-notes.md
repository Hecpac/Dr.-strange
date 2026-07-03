# Browser lane hardening — implementation notes

Última revisión: 2026-07-03

Bloque de dos PRs sobre la confiabilidad de browser_use (preflight CDP +
presupuesto LLM). Recon Fase 0 verificado contra el repo el 2026-07-03:
las tres rutas (delegada / interactiva / quick_screenshot), el gap de
preflight interactivo, y los defaults del paquete (llm_timeout=90s,
max_failures=5 en browser-use 0.11.13) quedaron confirmados con código
verbatim antes de implementar.

## PR 2 — presupuesto LLM explícito para browser_use (este PR)

### Problema

browser_use por defecto usa `llm_timeout=90` (modelos claude) y
`max_failures=5`. Bajo saturación sostenida del LLM externo, cada step
cuelga hasta que el `wait_for` de 90s cancela el retry, y el siguiente
step vuelve a empezar: hasta 5×90s ≈ 450s del presupuesto delegado
quemados antes de rendirse (observado ~10 min en el incidente del
2026-07-03 con Chrome zombie + 6× LLM timeout).

### Cambio

claw ahora pasa un presupuesto explícito y acotado a
`browser_use.Agent(...)`:

- `llm_timeout=60`, `max_failures=3` por defecto (delegada e interactiva
  por igual — ambas rutas convergen en el único call-site
  `ComputerHandler._run_browser_use_task` → `BrowserUseService.run_task`).
- Configurable vía env siguiendo el patrón existente de `AppConfig`:
  `CLAW_BROWSER_USE_LLM_TIMEOUT` (default 60) y
  `CLAW_BROWSER_USE_MAX_FAILURES` (default 3). Valores no-enteros caen al
  default (`_env_int`); valores ≤ 0 fallan `validate()` fail-fast.
- `BrowserUseService.run_task` acepta `llm_timeout` / `max_failures`
  opcionales (`None` = no pasar el kwarg, preservando los defaults del
  paquete para cualquier caller que no presupueste).

### Archivos cambiados

- `claw_v2/config.py` — campos `computer_browser_use_llm_timeout_seconds`
  / `computer_browser_use_max_failures`, lectura env, validación positiva.
- `claw_v2/computer.py` — `run_task(...)` acepta y reenvía
  `llm_timeout` / `max_failures` a `Agent(...)` (solo si no son `None`).
- `claw_v2/computer_handler.py` — constantes fallback
  `BROWSER_USE_LLM_TIMEOUT_SECONDS=60` / `BROWSER_USE_MAX_FAILURES=3`,
  accessors `_browser_use_llm_timeout()` / `_browser_use_max_failures()`
  (espejo de `_browser_use_timeout()`), y el paso de ambos valores en el
  call-site único de `run_task`.
- `tests/helpers.py` — los dos campos nuevos en el `AppConfig` manual.
- `tests/test_computer.py` — `BrowserUseLlmBudgetTests` (captura de
  kwargs de `Agent(...)`: valores pasados / omitidos→defaults del
  paquete), test delegado de defaults (60/3) y test de override de config
  (45/2) en el punto de convergencia.
- `tests/test_config.py` — `BrowserUseLlmBudgetConfigTests` (defaults,
  overrides env, env inválido→default, no-positivos rechazados).

### Comportamiento antes / después

| | antes | después |
|---|---|---|
| cuelgue por step (saturación) | 90s | 60s |
| steps fallidos consecutivos antes de terminar | 5 | 3 |
| presupuesto máximo quemado por racha de fallos | ~450s | ~180s |

El timeout largo de la operación delegada (1200s,
`_LONG_BROWSER_OPERATION_TIMEOUT_SECONDS`) queda intacto.

### Desviaciones del plan

Ninguna. El fallback cascade sigue apagado
(`BROWSER_USE_OAUTH_FALLBACK_MODEL = None`), `RetryChatAnthropic` sin
tocar, sin trabajo nuevo en `daemon.tick`, versión de browser-use sin
cambiar (0.11.13).

### Riesgos abiertos (intencionales)

- **PR 1 pendiente**: el probe de `BrowserCapability.ensure_ready()`
  sigue siendo solo `/json/version` — un Chrome zombie pasa el preflight
  delegado, y la ruta interactiva no tiene preflight en absoluto. Ese es
  el slice PR 1 (zombie detection + preflight interactivo), diseñado en
  el recon y aún no implementado.
- **Muerte de CDP mid-task** no se detecta claw-side (PR 3 / futuro).
- El singleton `_browser_svc` de `tools.py` cachea su preflight para
  siempre (anotado en el recon, fuera de scope).
- Tareas legítimamente lentas del LLM (>60s por step) ahora cortan antes;
  si aparece un falso positivo, el knob env permite subirlo sin deploy.

### Evidencia

- Tests dirigidos:
  `tests/test_computer.py::BrowserUseLlmBudgetTests` (2),
  `DelegatedBrowserTaskTests::test_delegated_browser_use_receives_default_llm_budget`,
  `DelegatedBrowserTaskTests::test_browser_use_task_forwards_configured_llm_budget`,
  `tests/test_config.py` completo — todo en verde.
- Suites completas `test_computer.py` + `test_config.py` +
  `test_architecture_invariants.py`: 176 passed; los 4 fallos de
  `DelegatedBrowserTaskTests` restantes son **pre-existentes en la base**
  (verificado con `git stash`: fallan igual sin este diff — dependen del
  entorno: la ruta determinista de x.com toca CDP real en :9250, que está
  caído).
- `ruff check` limpio; `ruff format --check` tiene drift **pre-existente**
  en 3 archivos (15 hunks idénticos con y sin este diff — no se
  reformateó código ajeno por la regla quirúrgica).
- mypy advisory: sin errores en las regiones tocadas.
- **Smoke en vivo pendiente**: el goal de este slice prohíbe reiniciar
  producción; per la regla de cierre del repo, este trabajo NO está
  cerrado hasta el smoke (`./scripts/restart.sh` + ejercer una tarea de
  navegador real y ver `llm_timeout`/`max_failures` efectivos).
