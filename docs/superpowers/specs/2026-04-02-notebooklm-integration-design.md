# NotebookLM Bot Integration — Design Spec

**Date:** 2026-04-02
**Branch:** feat/pending-items

## Goal

Give Claw direct control over NotebookLM via the `notebooklm-py` SDK, exposed as `/nlm_*` Telegram commands. Replace the fragile Chrome CDP automation with reliable API calls.

## Decisions

- **SDK direct** (not Chrome CDP) — authenticated via `~/.notebooklm/storage_state.json`, already working
- **Dedicated service** (`NotebookLMService`) in `claw_v2/notebooklm.py` — follows project pattern (PipelineService, SocialPublisher)
- **Explicit commands only** — no natural language routing for this iteration
- **Async with notification** — long operations (research, podcast) run in background threads and notify via Telegram on completion

## NotebookLMService

**File:** `claw_v2/notebooklm.py`

```python
class NotebookLMService:
    def __init__(
        self,
        notify: Callable[[str], None],
        observe: ObserveStream | None = None,
    ) -> None
```

- `notify` — callback that sends a Telegram message to the user
- `observe` — emits events to observe_stream for metrics
- Internally runs async SDK calls via a dedicated `asyncio` event loop in a thread
- Auth: `NotebookLMClient.from_storage()` using existing cookies

### Sync methods (fast, direct response)

| Method | Returns |
|--------|---------|
| `list_notebooks()` | `list[dict]` — id, title, created_at |
| `create_notebook(title)` | `dict` — id, title |
| `delete_notebook(notebook_id)` | `bool` |
| `list_sources(notebook_id)` | `list[dict]` — id, title, kind, url |
| `add_sources(notebook_id, urls)` | `list[dict]` — added sources |
| `add_text(notebook_id, title, content)` | `dict` — source info |
| `chat(notebook_id, question)` | `str` — notebook response |
| `status(notebook_id)` | `dict` — notebook info + sources + artifact state |

### Background methods (slow, notify on completion)

| Method | Immediate return | Notification |
|--------|-----------------|--------------|
| `start_research(notebook_id, query, mode="deep")` | `"Deep Research iniciado..."` | Sources imported count + notebook URL |
| `start_podcast(notebook_id)` | `"Generando podcast..."` | Completion status + notebook URL |

### Partial ID matching

All methods that accept `notebook_id` support partial IDs. The service fetches the notebook list and finds the first match where `id.startswith(partial_id)`. Error if zero or multiple matches.

### Background threading

```
command → validate notebook exists (sync) → spawn daemon Thread → return immediately
                                                   ↓
                                            run async operation
                                                   ↓
                                            notify(result or error)
```

**Protections:**
- One background operation per notebook at a time (`_running: dict[str, Thread]`)
- Always notify on error — never fail silently
- Timeouts: research 10 min, podcast 20 min

### Observe events

- `nlm_research_started` — payload: notebook_id, query, mode
- `nlm_research_completed` — payload: notebook_id, sources_count, duration_seconds
- `nlm_podcast_started` — payload: notebook_id
- `nlm_podcast_completed` — payload: notebook_id, duration_seconds
- `nlm_error` — payload: notebook_id, operation, error

## Bot Commands

New `/nlm_*` handlers in `bot.py`, following the `/chrome_*` and `/terminal_*` pattern.

| Command | Example | Response |
|---------|---------|----------|
| `/nlm_list` | `/nlm_list` | Table: id (short), title, date |
| `/nlm_create <title>` | `/nlm_create Noticias AI Abril` | `Notebook creado: {id} — {title}` |
| `/nlm_delete <id>` | `/nlm_delete bdf8` | `Notebook eliminado` |
| `/nlm_status <id>` | `/nlm_status bdf8` | Notebook info + sources + state |
| `/nlm_sources <id> <urls...>` | `/nlm_sources bdf8 https://... https://...` | List of added sources |
| `/nlm_text <id> <title> \| <content>` | `/nlm_text bdf8 Resumen \| El mercado...` | `Source de texto agregado` |
| `/nlm_research <id> <query>` | `/nlm_research bdf8 AI trends April` | `Deep Research iniciado...` → notification |
| `/nlm_podcast <id>` | `/nlm_podcast bdf8` | `Generando podcast...` → notification |
| `/nlm_chat <id> <question>` | `/nlm_chat bdf8 resume las fuentes` | Notebook response |

## Wiring in main.py

```python
from claw_v2.notebooklm import NotebookLMService

nlm_service = NotebookLMService(notify=send_fn, observe=observe)
bot.notebooklm = nlm_service
```

`send_fn` is the same Telegram send callback used by the bot's async notification path.

## Notification format

```
# Research success:
"Deep Research completado en notebook {title}
{N} fuentes importadas
https://notebooklm.google.com/notebook/{id}"

# Podcast success:
"Podcast generado para notebook {title}
https://notebooklm.google.com/notebook/{id}"

# Error:
"Error en {operation}: {error message}"
```

## Testing

**File:** `tests/test_notebooklm.py`

Mock the `NotebookLMClient` — no real API calls.

**Sync tests:**
- `test_list_notebooks` — returns formatted list
- `test_create_notebook` — returns id and title
- `test_delete_notebook` — returns confirmation
- `test_add_sources` — accepts multiple URLs
- `test_add_text` — parses title|content
- `test_chat` — returns notebook response
- `test_status` — returns consolidated info
- `test_partial_id_match` — `bdf8` resolves to full ID
- `test_partial_id_no_match` — clear error

**Background tests:**
- `test_research_starts_thread_and_notifies` — launches thread, returns immediately, calls notify
- `test_podcast_notifies_on_completion` — same pattern
- `test_background_error_notifies` — error triggers notify
- `test_one_operation_per_notebook` — rejects concurrent operation

**Bot command tests** (in `tests/test_bot.py` or `tests/test_notebooklm.py`):
- `test_nlm_list_command` — delegates to service
- `test_nlm_create_command` — parses title correctly
- `test_nlm_research_command` — parses id + query

## Files changed

| File | Change |
|------|--------|
| `claw_v2/notebooklm.py` | **New** — NotebookLMService |
| `claw_v2/bot.py` | Add `/nlm_*` command handlers + `notebooklm` attribute |
| `claw_v2/main.py` | Wire NotebookLMService into BotService |
| `tests/test_notebooklm.py` | **New** — unit tests |

## Out of scope

- Natural language routing ("crea un podcast sobre X")
- Downloading generated audio as MP3 to local disk
- NotebookLM video/report/quiz artifact generation (can be added later)
- Chrome CDP fallback
