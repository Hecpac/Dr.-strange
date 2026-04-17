# Codex CLI Integration Design

**Date:** 2026-04-06  
**Status:** Approved  
**Goal:** Aprovechar la suscripción ChatGPT Pro para reducir costos de API en el bot Dr. Strange.

---

## Context

El bot tiene dos suscripciones activas:
- **Claude Max 20x** — cubre Brain (Opus) y Worker via Claude Code CLI
- **ChatGPT Pro** — cubre Codex CLI sin costo por token

El único costo real de API hoy es **Computer Use** via OpenAI Responses API directa (`gpt-5.4`, `OPENAI_API_KEY`, pay-per-token).

El Worker ya está cubierto por Claude Max, pero enrutarlo por Codex libera headroom de la suscripción de Claude para el Brain.

---

## Architecture

```
Brain        → Claude Opus 4.6    (Claude Max 20x)   ← sin cambio
Worker       → Codex CLI          (ChatGPT Pro)       ← nuevo
Computer Use → Codex CLI          (ChatGPT Pro)       ← reemplaza OpenAI API directa
Fallback     → OpenAI API / Anthropic API             ← solo si Codex falla
```

---

## Components

### 1. `claw_v2/adapters/codex.py` — CodexAdapter

Nuevo ProviderAdapter que invoca `codex` CLI como subprocess.

- `provider_name = "codex"`
- `tool_capable = False`
- Invoca: `codex --model <model> --quiet <prompt>`
- Parsea stdout como texto de respuesta
- Config: `CODEX_CLI_PATH` (default: `codex`), `CODEX_MODEL` (default: `codex-mini-latest`)
- Maneja errores: CLI not found → `AdapterUnavailableError`, API error → `AdapterError`

### 2. `claw_v2/computer.py` — CodexComputerBackend

Nuevo backend de Computer Use que reemplaza el loop visual de OpenAI Responses API.

- Recibe la tarea en texto
- Invoca Codex CLI para generar AppleScript o bash
- Ejecuta el script localmente
- Fallback al loop visual existente si la tarea incluye keywords de visión (captcha, screenshot, UI dinámica)
- Seleccionable via `COMPUTER_USE_BACKEND=codex` (default: `openai`)

### 3. `claw_v2/config.py` — Nuevos campos

```python
codex_cli_path: str          # default: shutil.which("codex") or "codex"
codex_model: str             # default: "codex-mini-latest"
computer_use_backend: str    # default: "openai", options: "openai" | "codex"
```

### 4. `claw_v2/llm.py` — Registro del provider

Registrar `"codex"` en el `LLMRouter` para que `WORKER_PROVIDER=codex` funcione.

---

## Configuration (env vars)

```env
WORKER_PROVIDER=codex
WORKER_MODEL=codex-mini-latest
CODEX_CLI_PATH=codex
COMPUTER_USE_BACKEND=codex
```

El Brain no cambia (`BRAIN_PROVIDER=anthropic`, `BRAIN_MODEL=claude-opus-4-6`).

---

## Fallback Strategy

| Condición | Acción |
|---|---|
| Codex CLI no encontrado | `AdapterUnavailableError` → LLMRouter usa siguiente provider |
| Rate limit de ChatGPT Pro | Error 429 → fallback a Anthropic worker |
| Computer Use visual requerido | Detectado por keywords → usa OpenAI loop existente |
| Codex falla en script | Retorna error → bot reporta en Telegram |

---

## Out of Scope

- Brain no cambia (siempre Claude Opus)
- Cron jobs autónomos (wiki ingest, heartbeat) se quedan en Anthropic para no arriesgar el rate limit de Codex
- LinkedIn/Twitter social posting no cambia

---

## Testing

- Unit tests para `CodexAdapter.complete()` con mock subprocess
- Unit tests para `CodexComputerBackend` con mock Codex output
- Integration: verificar que `WORKER_PROVIDER=codex` enruta correctamente
