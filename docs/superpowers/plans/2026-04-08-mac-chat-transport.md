# Mac Chat Transport — Implementation Plan

> **For agentic workers:** Use this plan as the execution order. Do not build a native macOS app first. The first shipping milestone is a local web chat over the existing Python runtime. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local chat surface on Mac that reuses the existing agent core, supports links/images, exposes traces and approvals, and can later back a menubar app or SwiftUI client without reworking the backend.

**Primary decision:** `Local Chat API + localhost web UI` first. Menubar and SwiftUI are follow-on clients over the same local API.

**Dependencies:** Reuse the existing runtime (`build_runtime()`), `BotService`, `TelegramTransport` patterns, `observe`, `approval`, and managed Chrome browser context. Avoid introducing a frontend stack or a second agent runtime.

---

## Success Criteria

- [ ] A user can chat with the agent locally on Mac without Telegram.
- [ ] The local UI can send text, links, and images through the existing bot core.
- [ ] The local UI can list traces and show replay for a trace.
- [ ] The local UI can list approvals and approve/reject them.
- [ ] The local UI can list open tabs from the bot-managed Chrome and analyze a selected tab.
- [ ] Telegram keeps working unchanged as a separate transport.

---

## File Structure

| File | Responsibility |
|------|---------------|
| `claw_v2/chat_api.py` | **New** — local HTTP API over `BotService` |
| `claw_v2/web_transport.py` | **New** — localhost server lifecycle |
| `claw_v2/static/chat.html` | **New** — minimal local chat UI |
| `claw_v2/static/chat.js` | **New** — chat/traces/approvals/tabs wiring |
| `claw_v2/static/chat.css` | **New** — minimal styling |
| `claw_v2/config.py` | Add `web_chat_*` config flags |
| `claw_v2/lifecycle.py` | Start/stop web transport alongside Telegram |
| `claw_v2/bot.py` | Optional thin adapters for browser-context helpers if needed |
| `tests/test_chat_api.py` | **New** — API tests |
| `tests/test_web_transport.py` | **New** — transport lifecycle tests |
| `tests/test_lifecycle.py` | Update runtime startup tests |

---

## Phase 0 — Lock The Contract

### Outcome

The transport contract is defined before UI work starts.

### P0.1 Define local API contract

**Files:**
- Create: `claw_v2/chat_api.py`
- Create: `tests/test_chat_api.py`

- [ ] Define `POST /api/chat`
- [ ] Define `POST /api/multimodal`
- [ ] Define `GET /api/traces`
- [ ] Define `GET /api/traces/{trace_id}`
- [ ] Define `GET /api/approvals`
- [ ] Define `POST /api/approvals/{approval_id}/approve`
- [ ] Define `POST /api/approvals/{approval_id}/reject`
- [ ] Define `GET /api/browser/tabs`
- [ ] Define `POST /api/browser/analyze`
- [ ] Define `POST /api/browser/screenshot`

**Acceptance criteria:**
- [ ] Every route maps cleanly to existing runtime capabilities.
- [ ] No route bypasses `BotService` for core chat behavior.

### P0.2 Decide server implementation

**Recommendation:** `FastAPI`

**Why**
- small enough for a local tool
- clean JSON + multipart handling
- easy local startup
- future clients can consume the same API

- [ ] Add the server dependency only if needed for implementation.
- [ ] Keep the API loopback-only (`127.0.0.1`).

**Acceptance criteria:**
- [ ] The server can run locally without changing the agent’s domain model.

---

## Phase 1 — Ship The Local API

### Outcome

The agent has a local HTTP surface suitable for a browser UI and future native clients.

### P1.1 Chat endpoint

**Files:**
- Create: `claw_v2/chat_api.py`
- Create: `tests/test_chat_api.py`

- [ ] Implement `POST /api/chat`
- [ ] Accept `session_id` + `text`
- [ ] Route to `runtime.bot.handle_text(...)`
- [ ] Return `reply`
- [ ] Include `trace_id` when cheaply available from recent emitted events or response metadata

**Acceptance criteria:**
- [ ] Sending a text prompt to the API yields the same reply as the Telegram path.

### P1.2 Multimodal endpoint

**Files:**
- Modify: `claw_v2/chat_api.py`
- Modify: `tests/test_chat_api.py`

- [ ] Implement `POST /api/multimodal`
- [ ] Accept text + image upload
- [ ] Build content blocks compatible with `bot.handle_multimodal(...)`
- [ ] Return the assistant reply

**Acceptance criteria:**
- [ ] Image uploads from the local client reuse the same multimodal brain path as Telegram.

### P1.3 Trace endpoints

**Files:**
- Modify: `claw_v2/chat_api.py`
- Modify: `tests/test_chat_api.py`

- [ ] Implement `GET /api/traces?limit=N`
- [ ] Implement `GET /api/traces/{trace_id}`
- [ ] Reuse `observe.recent_events(...)` and `observe.trace_events(...)`

**Acceptance criteria:**
- [ ] A local client can inspect the same trace data already exposed via bot commands.

### P1.4 Approval endpoints

**Files:**
- Modify: `claw_v2/chat_api.py`
- Modify: `tests/test_chat_api.py`

- [ ] Implement `GET /api/approvals`
- [ ] Implement approve/reject endpoints
- [ ] Reuse the same approval manager that Telegram uses

**Acceptance criteria:**
- [ ] Pending actions can be resolved from the local client without Telegram.

### P1.5 Browser context endpoints

**Files:**
- Modify: `claw_v2/chat_api.py`
- Modify: `tests/test_chat_api.py`

- [ ] Implement `GET /api/browser/tabs`
- [ ] Implement `POST /api/browser/analyze`
- [ ] Implement `POST /api/browser/screenshot`
- [ ] Restrict scope to the bot-managed Chrome only
- [ ] Reuse `managed_chrome`, `browser.connect_to_chrome`, `chrome_navigate`, `chrome_screenshot`

**Acceptance criteria:**
- [ ] The local client can list tabs and analyze a selected tab without the user manually pasting the URL.

---

## Phase 2 — Add The Local Web UI

### Outcome

A usable localhost chat exists without introducing React, Next, Electron, or SwiftUI.

### P2.1 Minimal transcript UI

**Files:**
- Create: `claw_v2/static/chat.html`
- Create: `claw_v2/static/chat.js`
- Create: `claw_v2/static/chat.css`

- [ ] Build a basic chat transcript
- [ ] Add text input
- [ ] Add submit handling
- [ ] Render long responses cleanly

**Acceptance criteria:**
- [ ] A user can open `localhost` and send/receive messages comfortably.

### P2.2 Image upload

**Files:**
- Modify: `claw_v2/static/chat.html`
- Modify: `claw_v2/static/chat.js`

- [ ] Add image picker / drag-and-drop
- [ ] Send files to `POST /api/multimodal`

**Acceptance criteria:**
- [ ] The local UI supports image analysis without Telegram.

### P2.3 Traces + approvals side panels

**Files:**
- Modify: `claw_v2/static/chat.html`
- Modify: `claw_v2/static/chat.js`
- Modify: `claw_v2/static/chat.css`

- [ ] Add traces panel
- [ ] Add replay drawer/view
- [ ] Add approvals panel
- [ ] Add approve/reject actions

**Acceptance criteria:**
- [ ] The UI is already useful as an operator console, not just as a chat box.

### P2.4 Browser tabs panel

**Files:**
- Modify: `claw_v2/static/chat.html`
- Modify: `claw_v2/static/chat.js`

- [ ] Add `Tabs` panel
- [ ] Add refresh tabs action
- [ ] Add analyze selected tab action
- [ ] Add screenshot selected tab action

**Acceptance criteria:**
- [ ] The local chat can use browser context as a first-class workflow.

---

## Phase 3 — Wire It Into Runtime Lifecycle

### Outcome

The localhost chat starts and stops with the same runtime as Telegram.

### P3.1 Config flags

**Files:**
- Modify: `claw_v2/config.py`
- Modify: `tests/helpers.py` if present / relevant test setup files

- [ ] Add `web_chat_enabled`
- [ ] Add `web_chat_host`
- [ ] Add `web_chat_port`
- [ ] Add optional `web_chat_token`
- [ ] Keep defaults local-only and safe

**Suggested defaults**

```python
web_chat_enabled = True
web_chat_host = "127.0.0.1"
web_chat_port = 8765
web_chat_token = None
```

### P3.2 Web transport lifecycle

**Files:**
- Create: `claw_v2/web_transport.py`
- Modify: `claw_v2/lifecycle.py`
- Create: `tests/test_web_transport.py`
- Modify: `tests/test_lifecycle.py`

- [ ] Create `WebTransport` start/stop wrapper
- [ ] Start it in `lifecycle.run()` when enabled
- [ ] Stop it in `finally`
- [ ] Ensure one transport failing does not crash unrelated optional surfaces if graceful degradation is desired

**Acceptance criteria:**
- [ ] Telegram and local web chat can coexist in the same process.

---

## Phase 4 — Hardening

### Outcome

The local client is stable enough to use daily.

### P4.1 Session model

**Files:**
- Modify: `claw_v2/chat_api.py`
- Optional create: `claw_v2/session_store.py`

- [ ] Define default `session_id` strategy (`mac-main`, `mac-debug`, etc.)
- [ ] Allow multiple sessions from the local UI
- [ ] Keep session isolation aligned with current memory behavior

### P4.2 Local-only security

**Files:**
- Modify: `claw_v2/chat_api.py`
- Modify: `claw_v2/web_transport.py`

- [ ] Bind to `127.0.0.1` by default
- [ ] Add optional local token requirement
- [ ] Reject requests from non-loopback origins if applicable

### P4.3 Observability

**Files:**
- Modify: `claw_v2/observe.py`
- Modify: `claw_v2/chat_api.py`
- Modify: `tests/test_chat_api.py`

- [ ] Emit `web_chat_request`
- [ ] Emit `web_chat_latency`
- [ ] Emit `web_chat_browser_context_used`

**Acceptance criteria:**
- [ ] The new transport itself is observable and debuggable.

---

## Explicit Non-Goals For MVP

- [ ] No SwiftUI app yet
- [ ] No Electron/Tauri packaging yet
- [ ] No Safari/system-wide browser introspection
- [ ] No “read any window on the Mac” behavior
- [ ] No global hotkey
- [ ] No share extension

These can be phase-2/3 features after the local API proves useful.

---

## Recommended Execution Order

1. **P0.1** Contract
2. **P1.1** Chat endpoint
3. **P1.3** Traces
4. **P1.4** Approvals
5. **P2.1** Minimal chat UI
6. **P1.2** Multimodal
7. **P1.5** Browser context endpoints
8. **P2.3** Traces/approvals panels
9. **P2.4** Tabs panel
10. **P3.x** Lifecycle/config
11. **P4.x** Hardening

This order gets a useful local chat quickly, then adds operator features and browser context without blocking on a native app.

---

## Definition Of Done

The MVP is done when:

- [ ] Opening `http://127.0.0.1:8765` gives a working local chat
- [ ] Text and image prompts work
- [ ] Traces are inspectable
- [ ] Approvals can be resolved
- [ ] Browser tabs from bot-managed Chrome are visible and analyzable
- [ ] Telegram still works
- [ ] No domain logic was duplicated into the UI layer
