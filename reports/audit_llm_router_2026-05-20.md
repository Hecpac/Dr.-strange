# Cómo funciona el LLM Router — quién manda a quién y por qué se truncan las decisiones

**Archivo principal:** `claw_v2/llm.py`
**Soporte:** `claw_v2/config.py`, `claw_v2/brain.py`, `claw_v2/coordinator.py`, `claw_v2/hooks.py`, `claw_v2/retry_policy.py`, `claw_v2/adapters/{anthropic,openai,codex,google,ollama}.py`

---

## 1. Cuadro general — quién manda a quién

```
                          ┌───────────────────────────────┐
                          │           Telegram             │
                          │   (entra mensaje del user)     │
                          └──────────────┬─────────────────┘
                                         │
                                         ▼
                          ┌──────────────────────────────┐
                          │     bot.py:handle_text       │
                          │     dispatcher 15 handlers    │
                          │   (no_match → cae al brain)   │
                          └──────────────┬───────────────┘
                                         │ no_match
                                         ▼
                          ┌──────────────────────────────┐
                          │     BrainService.handle      │  ← brain.py:220
                          │   construye prompt + ctx     │
                          └──────────────┬───────────────┘
                                         │ router.ask(lane="brain")
                                         ▼
            ┌─────────────────────────────────────────────────────┐
            │                  LLMRouter.ask                       │  ← llm.py:55
            │  1. pick provider/model/effort/budget by lane         │
            │  2. pre_hooks (cost gate, anti-distillation)          │
            │  3. circuit_breaker.check(provider)                   │
            │  4. adapter.complete(request)                         │
            │  5. on AdapterError → _pick_fallback → re-ask         │
            │  6. post_hooks (decision_logger, sanitizers)          │
            └────────────┬───────────────────────┬─────────────────┘
                         │                       │
                         ▼                       ▼
         ┌──────────────────────┐  ┌──────────────────────────┐
         │ AnthropicAgentAdapter│  │  CodexAdapter / OpenAI… │
         │  (tool_capable=True) │  │   (subscription/CLI)     │
         └──────┬───────────────┘  └──────────────────────────┘
                │ tool calls reales
                ▼
        ┌──────────────────────────────┐
        │   ToolRegistry (Bash, Read,  │
        │   Write, Edit, Grep, MCP…)   │
        └──────────────────────────────┘

   Paralelo: CoordinatorService llama router.ask(lane="research"/"worker"/"worker_heavy")
             para descomponer tareas en research → synthesis → implementation → verification.

   Verifier / Judge se invocan desde plan_gate, task_handler, learning, kairos.
```

**Quién manda a quién:**

| Caller | Llama al router con lane | Para qué |
|---|---|---|
| `bot.py` → `brain.py` | `brain` | Responder al user / decidir el siguiente paso |
| `coordinator.py` | `research` → `worker` → `verifier` | Descomponer una tarea grande en sub-llamadas paralelas |
| `agents.py` (sub-agents) | `worker` o `worker_heavy` | Sub-agentes (alma, hex, rook, etc.) ejecutando skills |
| `plan_gate.py`, `task_handler.py` | `verifier` | Validar evidencia antes de cerrar como `succeeded` |
| `learning.py`, `dream.py`, `kairos.py` | `judge` / `worker` | Post-mortems, sueños, ticks proactivos |
| `morning_brief.py`, `eval.py` | `worker` | Reportes y evals |

El **brain NO instruye al worker directamente** — el coordinator es el que orquesta workers/research/verify. El brain decide *“esto vale la pena delegar”* y emite la decisión; otro servicio (coordinator/sub_agents/task_handler) hace el `router.ask(lane="worker")`. No hay un canal directo brain→worker; ambos pasan por el router como pares.

---

## 2. Las 6 lanes y para qué sirve cada una

`claw_v2/types.py:6`:
```python
Lane = Literal["brain", "worker", "worker_heavy", "verifier", "research", "judge"]
```

| Lane | Rol | Tool-capable | Default provider | Default model | Effort default |
|---|---|---|---|---|---|
| **brain** | Decide y conversa | sí | `anthropic` | `claude-opus-4-7` | `high` |
| **worker** | Ejecuta sub-tareas | sí | `anthropic` | `claude-sonnet-4-6` | `high` |
| **worker_heavy** | Sub-tareas pesadas / coding | sí | `codex` (ChatGPT plan) | `gpt-5.5` | `high` |
| **research** | Sintetiza información sin tools | no | `codex` (`research_provider`) | `gpt-5.5` | (hereda judge → `medium`) |
| **verifier** | Valida evidencia, blocking | no | derivado: `codex` si brain=anthropic, si no `anthropic` | (hereda) | (hereda judge → `medium`) |
| **judge** | Evaluador / critic / scoring | no | `codex` (default env) | `gpt-5.5` | `medium` |

**Reglas (config.py:608-667):**

- `provider_for_lane(lane)` (config.py:608) decide el provider:
  ```python
  critic_provider = "codex" if brain_provider == "anthropic" else "anthropic"
  mapping = {
    "brain": brain_provider,                          # default anthropic
    "worker": worker_provider,                        # default anthropic
    "worker_heavy": worker_heavy_provider,            # default codex
    "verifier": verifier_provider or critic_provider, # default codex (cuando brain=anthropic)
    "research": research_provider or brain_provider,  # default codex (env)
    "judge":    judge_provider or critic_provider,    # default codex
  }
  ```
- `model_for_lane(lane)` (config.py:620): explícito por env (`*_MODEL`) o cae a `advisory_model_for_provider(provider)`.
- `effort_for_lane(lane)` (config.py:643): `low/medium/high`. Verifier y research no tienen effort propio: heredan `judge_effort` (default `medium`). El brain está en `high`.
- `thinking_tokens_for_lane(lane)` (config.py:658): extended thinking de Anthropic. **Todos default 0** — ninguna lane tiene extended thinking activado a menos que pongas `BRAIN_THINKING_TOKENS`, etc.

**El “quién es crítico de quién” es deliberado:** Anthropic juzga a Codex y Codex juzga a Anthropic. Eso elimina la complicidad del mismo modelo opinando sobre sí mismo. Por eso `critic_provider` siempre es el contrario del `brain_provider`.

---

## 3. La función `LLMRouter.ask` paso a paso

`claw_v2/llm.py:55-199`. Cada vez que algo del sistema necesita una decisión LLM, llama `router.ask(...)`. Lo que pasa adentro:

### 3.1 Resolución de parámetros (`llm.py:75-106`)
- `selected_provider = provider or config.provider_for_lane(lane)` — el caller puede sobrescribir; si no, manda la config.
- `selected_model`, `selected_effort`, `selected_thinking` igual.
- `budget = effective_max_budget_for_request(...)`. Si el provider está en modo `subscription` (Claude Pro / ChatGPT plan), aplica un **piso** del subscription budget floor:
  ```python
  _SUBSCRIPTION_BUDGET_FLOORS = {
    "brain": 1.00, "worker": 0.25, "worker_heavy": 0.25,
    "verifier": 0.05, "research": 0.10, "judge": 0.05,
  }
  ```
  **El brain tiene budget mínimo 1 USD “notional” por turno**; el verifier solo 5 centavos. Para subscriptions ese floor no es coste real pero define cuántos tokens puede generar antes de que el adapter corte. **Este es uno de los puntos de truncamiento clave.**

### 3.2 Trace + validación (`llm.py:107-141`)
- Inyecta `trace_id/root_trace_id/span_id` en evidence_pack (para correlacionar todo en `observe_stream`).
- `request.validate()` lanza si el request es ilegal (p. ej. tool_capable=False con tools).

### 3.3 Pre-hooks
- `make_daily_cost_gate` (hooks.py:47): si el provider es API-billable y ya gastaste el daily limit (`MAX_BUDGET_USD` env, default 10 USD), **bloquea** con `LLMResponse(content="Request blocked by pre-hook…", confidence=0.0)`. Esa cadena llega tal cual al caller, **el caller NO la distingue de un fail real**.
- `make_anti_distillation_hook` (hooks.py:151): inyecta 2 “tool decoys” falsos en el system prompt para envenenar scrapers. Solo en lanes tool-capable.

### 3.4 Adapter compatibility check (`llm.py:144-145`)
```python
if lane not in NON_TOOL_LANES and not adapter.tool_capable:
    raise ValueError(f"Lane '{lane}' requires a tool-capable provider adapter.")
```
- `NON_TOOL_LANES = ("verifier","research","judge")`. Significa: **verifier, research y judge no pueden usar tools**. Solo razonan sobre el texto.
- Si el `provider_for_lane("worker")` apunta a Google (tool_capable=False), esto lanza ValueError antes de llamar al modelo. Por eso Google está solo en research/judge en la práctica.

### 3.5 Circuit breaker (`llm.py:201-240` + `retry_policy.py:90`)
- Antes de llamar al adapter: `circuit_breaker.check(provider)`.
- Si el provider llevó **3 fallos consecutivos**, se abre el circuito por **120 segundos** (`failure_threshold=3, cooldown_seconds=120`). En ese período el router lanza `AdapterUnavailableError`.
- Cuando el circuito se abre, el flujo cae directo a `_pick_fallback` — lo mismo que pasaría con un AdapterError real, pero sin haber hecho la llamada.

### 3.6 Llamada al adapter
- `adapter.complete(request)` ejecuta:
  - **Anthropic** (`adapters/anthropic.py`): usa Claude SDK / CLI, con session_id persistente. Aquí es donde se construyen los tool calls reales y donde se rompe con `Stream idle timeout` (vimos 484 errores en 3 días).
  - **Codex** (`adapters/codex.py`): invoca el CLI de ChatGPT Codex; tool loop interno, no expone tools individuales.
  - **OpenAI** (`adapters/openai.py`): API REST con tool_use opcional.
  - **Google / Ollama**: advisory only (no tools).

### 3.7 Fallback en AdapterError (`llm.py:147-182`)
```python
_FALLBACK_MAP = {"anthropic": "openai", "openai": "anthropic"}

def _pick_fallback(failed_provider, lane):
    if failed_provider == "codex":
        return None       # Codex no degrada a Claude jamás
    candidate = _FALLBACK_MAP.get(failed_provider)
    if candidate and candidate in self.adapters:
        return candidate
    if lane in NON_TOOL_LANES and failed_provider != "anthropic":
        return "anthropic"
    return None
```
- Anthropic falla → reintenta con OpenAI.
- OpenAI falla → reintenta con Anthropic.
- Codex falla → **no hay fallback**. La excepción sube al caller.

Después del retry, se marca `response.degraded_mode = True` y se emite `llm_fallback`. **Aquí es donde se cuela la basura `to=multi_tool_use.parallel …`** porque OpenAI a veces emite ese eco interno y el router solo aplica `redact_sensitive` (que sólo quita secretos shaped, no formato de tool-use).

### 3.8 Post-hooks (`llm.py:184-187`)
- `decision_logger` (hooks.py:88): emite `llm_decision` en `observe_stream` con prompt/response snapshot + confidence + cost. Esto es lo que ves en la DB.
- Otros post-hooks pueden añadirse para sanitización, redacción, etc.

### 3.9 Retorno
Devuelve `LLMResponse(content, lane, provider, model, confidence, cost_estimate, degraded_mode, artifacts)`.

---

## 4. Cómo el caller usa esa respuesta — y por qué se trunca

La decisión del router es un objeto, pero **el caller decide qué hacer con ella**:

### 4.1 BrainService (`brain.py:283-294`)
```python
response = self.router.ask(
    prompt,
    system_prompt=_brain_system_prompt(self.system_prompt),
    lane="brain",
    provider=model_override.provider if model_override else None,
    model=model_override.model if model_override else None,
    effort=model_override.effort if model_override else None,
    session_id=provider_session_id,
    evidence_pack=attach_trace({"app_session_id": session_id}, trace),
    max_budget=_resolve_max_budget(self.router),
    timeout=300.0,
)
```
- Pasa el `provider_session_id` para que Anthropic continúe la conversación.
- Si `provider_session_id is not None` (`resuming=True`), llama con `include_history=False` → el user prompt NO trae historial.
- Cuando AdapterError ocurre durante resume, intenta una sola vez con sesión fresca. Si vuelve a fallar, sube.

### 4.2 CoordinatorService (`coordinator.py:357`)
```python
response = self.router.ask(task.instruction, **kwargs)
```
- `_RETRY_LANES = frozenset({"worker","worker_heavy"})` → solo worker/worker_heavy reintenta 2 veces. Verifier/research/judge reintentan UNA SOLA VEZ.
- Lanza tareas con `ThreadPoolExecutor(max_workers=4)` en paralelo. **4 lanes en simultáneo**.
- **Trunca el contexto entre fases**:
  - `WORKER_RESULT_SUMMARY_CHARS = 900` (coordinator.py:16) — cada resultado de worker se resume a 900 chars antes de pasarlo al synthesizer.
  - `PHASE_INPUT_SUMMARY_CHARS = 1_500` — todo el input para la synthesis cap a 1.5 KB.
- Resultado: el brain dispara un trabajo de 10 minutos con 4 workers, recibe la síntesis resumida en 1.5 KB y **ya no ve los detalles**. Por eso a veces declara “verificado” sobre evidencia que en realidad falló — los detalles se quedaron en el WorkerResult original, no en la síntesis.

### 4.3 plan_gate / task_handler (verifier)
- `router.ask(lane="verifier", evidence_pack={...}, max_budget=0.05)`.
- Verifier NO tiene tools — solo razona sobre el `evidence_pack`. Si el evidence_pack tiene 267 chars (lo que vimos en producción), está juzgando casi a ciegas.
- La respuesta del verifier va a `task_handler._maybe_run_petri_verifier` y decide si la fila pasa a `verification_status='passed'` o queda en `needs_verification`.
- **Si el verifier devuelve `(unused)` con confidence 0** (33% de las veces), el task queda `needs_verification` para siempre → watchdog lo mata como `lost`.

---

## 5. Cinco puntos donde el router **trunca** decisiones

### 5.1 Subscription budget floor → cap de tokens de output
`config.py:79-86`. Si el `worker_heavy` corre con codex (subscription), el floor de 0.25 USD se traduce internamente en un cap de tokens. Pedirle a worker_heavy una refactorización grande con `MAX_BUDGET_USD=10` parece dar holgura, pero el floor por-call es 0.25 — eso son ~50K tokens de output como mucho. Tareas más grandes salen **cortadas** en mitad de un patch.

### 5.2 `_brain_system_prompt` agrega 8 contratos al system prompt
`brain.py:1202-1213`. Concatena BRAIN_RESPONSE_CONTRACT + CONVERSATIONAL_STYLE + PUSHBACK + SELF_HEALING + AUTONOMY_EXECUTION + RUNTIME_OPERATIONS + CAPABILITY_DENIAL + IDENTITY_ANCHOR. Eso son ~6 KB extra por turno además de los ~62 KB del workspace context. Resultado: **el prompt real está al 94% saturado por instrucciones**, no por conversación.

### 5.3 `_RETRY_LANES` excluye verifier/research/judge
`coordinator.py:326`. Si el verifier falla, no se reintenta. Si el primer intento devuelve `(unused)`, esa tarea **no tiene segunda oportunidad** — el caller cree que la verificación corrió y devuelve `confidence=0`.

### 5.4 `PHASE_INPUT_SUMMARY_CHARS=1.500` y `WORKER_RESULT_SUMMARY_CHARS=900`
`coordinator.py:16-17`. Cada research worker puede generar 50K de texto. El coordinator los resume a 900 chars cada uno antes de pasarlos al synthesizer, y la entrada total al synthesizer cap a 1.5 KB. Si 4 workers producen 50K cada uno (200K total), el synthesizer ve **1.5 KB** y opina sobre eso.

### 5.5 Circuit breaker abre durante 120s al tercer fallo
`retry_policy.py:90-92`. Cuando Anthropic da 3 timeouts (frecuente en horas pico), durante 2 minutos **todo lane que apunte a anthropic** cae a fallback (OpenAI). Eso afecta brain + worker + parte de verifier. En esos 2 minutos:
- el brain responde con un modelo distinto (gpt-5.5 vía OpenAI o codex)
- pierde el `provider_session_id` Anthropic (la sesión es por-provider)
- al volver, Anthropic ve una conversación interrumpida y a veces re-resume mal

---

## 6. Anomalía en producción que confirma 5.5 + bug de logging

DB últ 3 días, lane×provider×model en `llm_decision`:

| Lane | Provider | Model | Decisiones |
|---|---|---|---:|
| judge | codex | gpt-5.5 | 470 |
| brain | anthropic | claude-opus-4-7 | 232 |
| research | codex | gpt-5.5 | 80 |
| **research** | **anthropic** | **gpt-5.5** | **62** |
| **verifier** | **anthropic** | **gpt-5.5** | **31** |
| **worker** | **anthropic** | **gpt-5.5** | **31** |
| worker | anthropic | claude-opus-4-7 | 14 |
| worker | anthropic | claude-sonnet-4-6 | 13 |
| worker | codex | gpt-5.5 | 9 |
| verifier | codex | gpt-5.5 | 6 |
| worker | openai | gpt-5.5 | 4 |

Las filas en negrita son **inconsistentes**: `provider=anthropic + model=gpt-5.5`. Anthropic no sirve modelos GPT. Hipótesis:
- El fallback corrió Anthropic→OpenAI, pero `decision_logger` (hooks.py:88) está leyendo `response.lane/provider/model` y el `response.provider` no se actualiza al provider del fallback. Bug en el path de fallback en `llm.py:166` (solo marca `degraded_mode=True` pero no rota el provider en la response).

**Impacto:** los dashboards y `observe_stream` mienten sobre quién contestó. Cuando el brain dice “consulté al verifier y pasó”, la DB dice que Anthropic respondió pero realmente fue OpenAI.

---

## 7. Por qué las decisiones se sienten “cortadas” entre brain ↔ worker ↔ verifier

La cadena real cuando el bot decide algo complejo:

1. **Brain** decide: “esto necesita 3 workers de research + 1 verifier”.
2. Llama (a través del task_handler o coordinator) → CoordinatorService.
3. CoordinatorService lanza 3 workers en paralelo (router.ask × 3, lane=research). Cada uno genera 30 KB de texto.
4. Coordinator **resume cada uno a 900 chars** y junta los 3 en `findings` (<=1.500 chars).
5. Llama al synthesizer (router.ask, lane=research). Synthesizer ve solo 1.5 KB.
6. Synthesizer devuelve un plan numerado de pasos.
7. Si hay implementation_tasks, lanza otros workers (lane=worker). Mismo cap.
8. Llama al verifier (router.ask, lane=verifier, evidence_pack pequeño). Verifier no tiene tools, solo razona sobre el evidence_pack.
9. Coordinator devuelve `CoordinatorResult.synthesis` al brain.
10. Brain ve la síntesis y **NO los WorkerResult detallados**. Decide la respuesta al user en base a eso.

**En cada flecha de arriba hay un cap.** Por eso el brain dice cosas como “verifiqué que X funciona” cuando en realidad un worker registró un error en su content de 30 KB que se cortó a 900 chars y nunca llegó al brain.

---

## 8. La cadena de FALLOS típica que ya tienes en producción

1. Hector escribe en Telegram.
2. `bot.handle_text` itera 15 handlers, 14 dan `no_match`, último cae a brain.
3. `BrainService` arma prompt con 17 K tokens de system + 1 K de user. `include_history=False` porque hay `provider_session_id` activo.
4. `router.ask(lane="brain", provider=None)` → `provider_for_lane("brain")` = `anthropic`.
5. Circuit_breaker abierto por Anthropic en cooldown → AdapterUnavailableError → `_pick_fallback("anthropic","brain")` = `"openai"`.
6. OpenAI responde con `to=multi_tool_use.parallel 天天中彩票软件 {…}` (eco interno). El router solo aplica `redact_sensitive` (no atrapa esto), emite `llm_fallback`, devuelve content corrupto.
7. Brain devuelve `response.content` al bot.
8. Bot lo envía a Telegram (a veces el detector de `_INTERNAL_TOOL_TRACE_PATTERNS` en `brain.py:89-94` lo atrapa, a veces no).
9. En paralelo, el ledger `brain-tooluse:tg-…:…` queda `running + needs_verification`.
10. El verifier corre → devuelve `(unused), confidence=0` (33% de las veces).
11. Watchdog a los 300s → marca la fila `lost / runtime lost authoritative backing state`.
12. La próxima vez que Hector pregunta “qué pasó con eso”, el brain consulta el ledger y dice “falló” — aunque la respuesta sí salió.

---

## 9. Recomendaciones para que decida mejor

**Caps que conviene cambiar:**

| Cap | Archivo:línea | Actual | Recomendado |
|---|---|---|---|
| `PHASE_INPUT_SUMMARY_CHARS` | coordinator.py:17 | 1.500 | 6.000 |
| `WORKER_RESULT_SUMMARY_CHARS` | coordinator.py:16 | 900 | 4.000 |
| `_RETRY_LANES` | coordinator.py:326 | `{worker, worker_heavy}` | añadir `verifier, research, judge` |
| Subscription floor `verifier` | config.py:83 | 0.05 | 0.20 (más output disponible) |
| `failure_threshold` circuit breaker | retry_policy.py:91 | 3 | 5 (menos sensible a 1-2 timeouts puntuales) |
| `cooldown_seconds` | retry_policy.py:92 | 120 | 30 (recupera más rápido cuando Anthropic vuelve) |

**Cambios estructurales:**

1. **Arreglar el bug del fallback**: en `llm.py:155-167` cuando se hace fallback, actualizar `response.provider` y `response.model` al provider real que respondió, no dejar el original. Hoy los logs mienten.
2. **Sanitizar fallback OpenAI**: en el path `llm.py:fallback`, aplicar el detector `_INTERNAL_TOOL_TRACE_PATTERNS` antes de devolver el response (hoy solo está en brain.py para el path normal).
3. **Distinguir “bloqueado por pre-hook” de “fallo de adapter”**: hoy el caller recibe ambos como `LLMResponse(confidence=0)`. Que pre-hook block sea una excepción tipada (`PreHookBlocked`) para que brain no la confunda con un fail.
4. **Pasar más evidence al verifier**: el evidence_pack vive en 267 chars (66 tokens). Subirlo a ~2 KB con los últimos 5 task_outcomes + 3 facts top — el verifier necesita evidencia para no devolver `(unused)`.
5. **Dejar de truncar workers a 900 chars antes del synthesizer**. O resumir con un modelo barato (haiku) en vez de un cap fijo.
6. **No usar fallback en lane=brain**. El brain es decisor; degradarlo silenciosamente a otro provider con otro identity-conditioning explica el `Soy Dr. Strange en el daemon local…` que aparece a destiempo (OpenAI no fue cargado con el mismo system prompt que Anthropic estaba usando con caching).

---

## 10. Resumen visual de la cadena

```
USER  ─┐
       │
       ▼                                    NON_TOOL_LANES
   BOT.handle_text                          ─────────────
       │                                    verifier
       │ no_match                           research
       ▼                                    judge
   BRAIN.handle_message                     (no tools)
       │
       │ router.ask(lane="brain")
       ▼
   ┌──────── LLMRouter.ask ────────┐
   │                                │
   │  config.provider_for_lane()    │  ←─ env: BRAIN_PROVIDER, etc.
   │  config.model_for_lane()       │
   │  config.effort_for_lane()      │
   │  effective_max_budget()        │  ←─ subscription floors
   │  pre_hooks (cost, decoy)       │
   │  circuit_breaker.check()       │  ←─ 3 fallos → 120s open
   │  adapter.complete()            │
   │     │                          │
   │     ├─ ok → post_hooks         │
   │     │       │                  │
   │     │       └─ decision_logger │
   │     │                          │
   │     └─ error → _pick_fallback  │  ←─ anthropic↔openai
   │              re-ask            │      codex no degrada
   │              degraded_mode=True│
   └────────────┬───────────────────┘
                │
                ▼
   ┌──── CoordinatorService.run ────┐
   │ research workers (paralelo)    │  ─→ router.ask × N (lane=research)
   │ ↓ cap 900 chars c/u            │
   │ synthesizer                     │  ─→ router.ask (lane=research, cap 1.5KB input)
   │ ↓                               │
   │ implementation workers (par.)  │  ─→ router.ask × N (lane=worker)
   │ ↓ cap 900 chars c/u            │
   │ verification workers           │  ─→ router.ask × N (lane=verifier, sin tools)
   │ ↓                               │
   │ CoordinatorResult               │
   └────────────┬───────────────────┘
                │ vuelve al brain con SOLO la síntesis
                ▼
            response al USER
```

La regla mental: **el brain manda decisiones; el coordinator manda ejecuciones; el router solo es plomería que enruta provider/model/budget y aplica hooks**. La conversación se trunca porque el plomero pasa los resultados resumidos, no los originales.
