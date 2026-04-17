# Pre/Post LLM Hooks — Design Spec

**Date:** 2026-03-28
**Status:** Approved
**Branch:** feat/pending-items

## Goal

Add a lightweight hook system to `LLMRouter` that runs callbacks before and after every LLM call. Initial hooks: daily cost gate ($10/day global) and structured decision logging for backtesting.

## Approach

Callbacks as typed callables injected into `LLMRouter`. No new classes, no plugin registry — just functions composed via closures.

## Hook Types

```python
# In adapters/base.py
PreLLMHook = Callable[[LLMRequest], LLMRequest | None]   # None = block request
PostLLMHook = Callable[[LLMRequest, LLMResponse], LLMResponse]
```

- **Pre-hooks** run after the `LLMRequest` is built but before `adapter.complete()`. They can mutate the request or return `None` to block it.
- **Post-hooks** run after `adapter.complete()` but before the existing `audit_sink`. They can mutate the response.

## Flow in LLMRouter.ask()

```
1. Build LLMRequest (existing)
2. Run pre-hooks sequentially:
   - request = hook(request)
   - If None → return blocked LLMResponse immediately
3. adapter.complete(request) (existing)
4. Run post-hooks sequentially:
   - response = hook(request, response)
5. audit_sink (existing)
6. Return response
```

When a pre-hook blocks, the response is:

```python
LLMResponse(
    content="Request blocked: daily cost limit ($10.00) reached.",
    lane=request.lane,
    provider="none",
    model="none",
    confidence=0.0,
    cost_estimate=0.0,
    artifacts={"blocked_by": hook_name},
)
```

No exception raised — the user sees the message as a normal Telegram reply.

## Hook 1: Daily Cost Gate

Factory function in `claw_v2/hooks.py`:

```python
def make_daily_cost_gate(observe: ObserveStream, daily_limit: float = 10.0) -> PreLLMHook:
```

- Calls `observe.total_cost_today()` which sums `cost_estimate` from `observe_stream` where `event_type = 'llm_response'` and `timestamp >= today 00:00 UTC`.
- If total >= limit, returns `None` (blocked).
- Otherwise returns the request unchanged.

### ObserveStream.total_cost_today()

New method on `ObserveStream`:

```python
def total_cost_today(self) -> float:
    # SELECT COALESCE(SUM(json_extract(payload, '$.cost_estimate')), 0.0)
    # FROM observe_stream
    # WHERE event_type = 'llm_response'
    #   AND timestamp >= date('now', 'start of day')
```

Uses the existing `observe_stream` table. No schema changes.

## Hook 2: Structured Decision Logger

Factory function in `claw_v2/hooks.py`:

```python
def make_decision_logger(observe: ObserveStream) -> PostLLMHook:
```

Emits an `llm_decision` event to `observe_stream` with payload:

| Field | Source |
|-------|--------|
| `session_id` | `request.session_id` |
| `confidence` | `response.confidence` |
| `cost_estimate` | `response.cost_estimate` |
| `degraded_mode` | `response.degraded_mode` |
| `prompt_length` | `len(request.prompt)` for str, `len(request.prompt)` (block count) for list |
| `response_length` | `len(response.content)` |
| `effort` | `request.effort` |
| `has_evidence_pack` | `request.evidence_pack is not None` |

Distinct from the existing `llm_response` audit event: `llm_decision` captures quality/analysis data, not operational data.

## Config

New field in `AppConfig`:

```python
daily_cost_limit: float  # env: DAILY_COST_LIMIT, default: 10.0
```

## Wiring in main.py

```python
from claw_v2.hooks import make_daily_cost_gate, make_decision_logger

pre_hooks = [make_daily_cost_gate(observe, config.daily_cost_limit)]
post_hooks = [make_decision_logger(observe)]

router = LLMRouter.default(
    config,
    ...,
    pre_hooks=pre_hooks,
    post_hooks=post_hooks,
)
```

`LLMRouter.default()` forwards `pre_hooks` and `post_hooks` to the constructor.

## Files Changed

| File | Change |
|------|--------|
| `claw_v2/adapters/base.py` | Add `PreLLMHook`, `PostLLMHook` type aliases |
| `claw_v2/llm.py` | Accept and run hooks in `LLMRouter` |
| `claw_v2/hooks.py` | **New** — `make_daily_cost_gate`, `make_decision_logger` |
| `claw_v2/observe.py` | Add `total_cost_today()` |
| `claw_v2/config.py` | Add `daily_cost_limit` |
| `claw_v2/main.py` | Wire hooks in `build_runtime()` |
| `tests/helpers.py` | Add `daily_cost_limit` to `make_config()` |
| `tests/test_hooks.py` | **New** — unit tests for both hooks |
| `tests/test_runtime.py` | Integration test: hooks + router |

## Testing Strategy

- **Cost gate:** Create an `ObserveStream` with test DB, emit fake `llm_response` events with known costs, verify gate blocks when limit is exceeded.
- **Decision logger:** Call the hook with a fake request/response, verify the `llm_decision` event was emitted with correct payload.
- **Integration:** Build a runtime with hooks, send a message, verify both hooks ran (cost gate passed, decision logged).
- **Blocked response:** Verify that when cost gate blocks, the returned `LLMResponse` has the correct content and artifacts.
