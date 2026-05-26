# Audit hardening — success_condition + Tier 3 preflight + memory revalidation

**Goal:** un task no se marca `succeeded` solo porque una tool devolvió `ok=True`. El verifier debe validar estado final semántico contra un contrato declarado per-tool (`success_condition`). Tier 3 adicionalmente debe tener preflight DOM/cuenta + revalidar memoria load-bearing antes de actuar.

**Driven by:** 2 fallas reales hoy 2026-05-26 — (a) X compose-thread `tool.ok=True` pero solo posteó T8 huérfano, (b) `@PachanoDesign` stale en memoria llevó a publish target equivocado.

---

## 1. Schema `success_condition`

Extensión de `ToolDefinition` en `claw_v2/tools.py:259`. Cada tool declara:

```python
@dataclass(slots=True, frozen=True)
class SuccessCondition:
    """Declarative contract for what 'succeeded' means for this tool.

    The verifier evaluates these after handler returns ok=True.
    At least one of `must_contain_keys`, `external_check`, `state_delta_check`
    must be non-trivial for Tier 2+; Tier 3 requires `external_check`.
    """
    must_contain_keys: tuple[str, ...] = ()       # result dict must have these keys (truthy)
    must_match_regex: dict[str, str] = field(default_factory=dict)  # field_path → regex
    external_check: ExternalCheckSpec | None = None  # see below — tier 3 mandatory
    state_delta_check: StateDeltaSpec | None = None  # what changed must be observable
    forbidden_reasons: tuple[str, ...] = ()       # result must NOT contain these reason strings


@dataclass(slots=True, frozen=True)
class ExternalCheckSpec:
    """Verifier re-fetches external state to confirm the change.

    For browser/CDP actions: re-navigate + scan for expected_phrases.
    For API actions: hit a verify endpoint and parse response.
    """
    kind: Literal["cdp_phrase", "http_get_json", "filesystem_glob", "gh_api", "sqlite_row"]
    target: str                                   # URL, path, sql query, etc.
    expected_phrases: tuple[str, ...] = ()        # must ALL appear in scanned text
    forbidden_phrases: tuple[str, ...] = ()       # must NOT appear
    json_path_equals: dict[str, Any] | None = None   # JSONPath → expected value
    settle_seconds: float = 6.0                   # wait before re-fetch
    timeout_seconds: float = 30.0                 # max retry window
    retries: int = 3


@dataclass(slots=True, frozen=True)
class StateDeltaSpec:
    """Sanity check that observable state changed."""
    db_table: str | None = None
    expected_rows_delta: int = 1                  # >= this many new rows
    fs_path: str | None = None
    expected_size_delta_bytes: int = 1            # file grew by >= this


@dataclass(slots=True, frozen=True)
class PreflightSpec:
    """MANDATORY for Tier 3 tools touching DOM / external accounts / publish / deploy."""
    probe_kind: Literal["dom_selector_present", "auth_check", "account_exists", "branch_check", "dry_run"]
    target: str
    selectors: tuple[str, ...] = ()               # for dom_selector_present
    must_match: dict[str, Any] = field(default_factory=dict)
    fail_message: str = "preflight failed"
```

**Extended `ToolDefinition`:**

```python
@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    allowed_agent_classes: tuple[AgentClass, ...]
    handler: ToolHandler
    mutates_state: bool = False
    requires_network: bool = False
    parameter_schema: dict | None = None
    ingests_external_content: bool = False
    sanitize_fields: tuple[str, ...] = ()
    tier: int = DEFAULT_TOOL_TIER
    # NEW
    success_condition: SuccessCondition | None = None   # required for tier >= 2
    preflight: PreflightSpec | None = None              # required for tier == 3
    memory_load_bearing_keys: tuple[str, ...] = ()      # memory keys to revalidate before tier 3
```

---

## 2. Terminal state semantics (durable)

| Estado | Cuándo |
|---|---|
| `pending_verification` | Tool returned ok=True. SuccessCondition declared but external_check still in flight or partial. Default landing state for Tier 2/3 con success_condition. |
| `succeeded` | Tool returned ok=True AND success_condition fully validated (must_contain_keys OK, external_check pass, no forbidden_reasons). |
| `failed` | Tool returned ok=False OR success_condition violated OR forbidden_reasons matched OR preflight rejected. |
| `blocked` | Approval gate not satisfied, preflight failed reversibly, sandbox blocked, external dep missing. User input required. |
| `lost` | Daemon restart killed task in-flight; reconciliation cannot recover. |

**Hard rule:** transition `pending_verification → succeeded` ONLY by `verifier_pass_external_check()`. No code path may shortcut.

---

## 3. Memory load-bearing revalidation (Tier 3 only)

Cada tool Tier 3 declara `memory_load_bearing_keys = ("x_handle", "github_repo", ...)`. Antes de ejecutar:

```python
def revalidate_memory_claims(claims: tuple[str, ...]) -> RevalidationResult:
    """For each claim, run its declared verifier and refresh MEMORY.md if stale.

    Example: claim 'x_handle' → CDP GET x.com/{handle} → if accountExists=False → BLOCK.
    """
```

Mantengo registro `MEMORY_CLAIM_VALIDATORS` (dict) con verifiers concretos por claim:

```python
MEMORY_CLAIM_VALIDATORS: dict[str, MemoryClaimValidator] = {
    "x_handle": validate_x_handle,         # CDP probe x.com/{handle} → accountExists
    "github_repo": validate_gh_repo,       # gh api repos/{owner}/{repo}
    "linkedin_logged_in": validate_li_auth, # CDP check li nav
    "telegram_chat_id": validate_tg_chat,  # bot API getChat
    ...
}
```

Si el validator dice "stale", el agent debe (a) actualizar `MEMORY.md` con la corrección, (b) **BLOCKEAR** la acción Tier 3 hasta que Hector confirme nuevo valor, NO inferir un default.

---

## 4. Tier 3 preflight obligatorio

Cualquier `ToolDefinition` con `tier == 3` SIN `preflight` no se registra:

```python
def register(self, definition: ToolDefinition) -> None:
    if definition.tier >= 2 and definition.success_condition is None:
        raise ToolRegistrationError(
            f"Tool {definition.name!r} tier={definition.tier} requires success_condition"
        )
    if definition.tier == 3 and definition.preflight is None:
        raise ToolRegistrationError(
            f"Tier 3 tool {definition.name!r} requires preflight spec"
        )
    self._tools[definition.name] = definition
```

Preflight runs ANTES del handler. Si `preflight.probe_kind == "dom_selector_present"` y los selectores no aparecen en `settle_seconds`, → status=`blocked`, reason=`preflight_dom_missing`, NO se ejecuta el handler.

**Aplicado a X compose-thread (la falla de hoy):**

```python
PreflightSpec(
    probe_kind="dom_selector_present",
    target="https://x.com/compose/post",
    selectors=("[data-testid='addButton']",),   # the "+" add-tweet button
    fail_message="X compose-thread '+' button not found — DOM changed, abort before posting"
)
```

Si preflight hubiera corrido hoy → bloqueo Tier 3 antes de quemar el "Postear" botón → cero T8 huérfano.

---

## 5. Archivos a tocar (4)

| File | Cambios |
|---|---|
| `claw_v2/tools.py:259-270` | Extender `ToolDefinition` con 3 campos nuevos. Add dataclasses `SuccessCondition`, `ExternalCheckSpec`, `StateDeltaSpec`, `PreflightSpec` |
| `claw_v2/tools.py` (registry) | Agregar validación en `register()` que rechaza tier>=2 sin success_condition y tier==3 sin preflight |
| `claw_v2/coordinator_schema.py:178-202` | Add `validate_success_condition(result, definition)` con todas las checks. Llamarlo desde `validate_coordinator_semantics`. |
| `claw_v2/task_handler.py:754-758` | Cambiar `terminal_status = "succeeded" if verification_status == "passed"` para que requiera `success_condition.evaluate(result) == OK`. Default a `pending_verification` si no se puede validar. |
| `claw_v2/memory_revalidation.py` (NEW) | Módulo con `MEMORY_CLAIM_VALIDATORS` + `revalidate_memory_claims()`. Llamado desde dispatcher tier 3. |
| `claw_v2/preflight.py` (NEW) | Runner de `PreflightSpec.probe_kind` con handlers para `dom_selector_present`, `auth_check`, etc. |

---

## 6. Test mínimo demostrando que `tool_ok=True` ya no basta

`tests/test_success_condition.py` (NEW):

```python
"""Regression test: tool_ok=True alone NEVER promotes a task to succeeded.

This codifies the 2026-05-26 incident where the X compose-thread tool reported
ok=True ("Postear" clicked) but only T8 of an 8-tweet thread was actually posted.
The new success_condition contract should catch this class of failure.
"""
from __future__ import annotations

import pytest

from claw_v2.tools import (
    SuccessCondition,
    ExternalCheckSpec,
    ToolDefinition,
    ToolRegistry,
    AgentClass,
)
from claw_v2.coordinator_schema import validate_success_condition


def _fake_handler_ok(args):
    return {"ok": True, "tweets_intended": 8, "tweets_posted_count": 1}


def test_registry_rejects_tier3_without_success_condition():
    reg = ToolRegistry()
    bad = ToolDefinition(
        name="DangerouslyPublish",
        description="...",
        allowed_agent_classes=("deployer",),
        handler=_fake_handler_ok,
        tier=3,
        mutates_state=True,
        # MISSING success_condition + preflight
    )
    with pytest.raises(Exception) as excinfo:
        reg.register(bad)
    assert "success_condition" in str(excinfo.value) or "preflight" in str(excinfo.value)


def test_tool_ok_alone_does_not_pass_when_external_check_fails():
    """The 2026-05-26 X compose-thread incident, codified."""
    sc = SuccessCondition(
        must_contain_keys=("tweets_posted_count",),
        external_check=ExternalCheckSpec(
            kind="cdp_phrase",
            target="https://x.com/HectorPach71777",
            expected_phrases=(
                "Singapur acaba de poner número",   # T1
                "5 cosas cambian cuando un gobierno", # T2
                "El mercado votó por routing",        # T3
                "esos son los nuevos logs",           # T4
                "Es la capa de política alrededor",   # T5
                "deja de sonar absurdo",              # T6
                "Es un número de planificación",      # T7
                "cuando hay 219 más",                 # T8
            ),
        ),
    )

    tool_result = {"ok": True, "tweets_intended": 8, "tweets_posted_count": 1}
    # Simulated CDP scan of profile after submit — only T8's phrase present
    observed_body_text = "Dejó de ser '¿puede funcionar?'. Ahora es: 'cuando hay 219 más'"

    errors = validate_success_condition(
        tool_result=tool_result,
        condition=sc,
        external_observation={"body_text": observed_body_text},
    )
    # tool_ok=True but only 1 of 8 expected phrases present → must fail
    assert errors, "tool_ok=True with partial external state should NOT pass success_condition"
    assert any("expected_phrase_missing" in e for e in errors)


def test_success_condition_pass_when_all_phrases_present():
    sc = SuccessCondition(
        must_contain_keys=("tweets_posted_count",),
        external_check=ExternalCheckSpec(
            kind="cdp_phrase",
            target="https://x.com/HectorPach71777",
            expected_phrases=("hello", "world"),
        ),
    )
    tool_result = {"ok": True, "tweets_posted_count": 2}
    observed_body_text = "saying hello to the world right now"
    errors = validate_success_condition(
        tool_result=tool_result,
        condition=sc,
        external_observation={"body_text": observed_body_text},
    )
    assert not errors


def test_forbidden_reason_blocks_success():
    sc = SuccessCondition(
        forbidden_reasons=("no_editor", "preflight_failed"),
    )
    tool_result = {"ok": True, "reason": "no_editor", "len": 0}
    errors = validate_success_condition(
        tool_result=tool_result,
        condition=sc,
        external_observation={},
    )
    assert any("forbidden_reason_matched" in e for e in errors)


def test_memory_load_bearing_keys_must_revalidate_before_tier3(monkeypatch):
    """Tier 3 publish to X handle should re-verify the handle is current."""
    from claw_v2 import memory_revalidation as mr

    monkeypatch.setattr(mr, "validate_x_handle", lambda handle: {"valid": False, "reason": "account_does_not_exist", "claimed": handle})
    result = mr.revalidate_memory_claims(("x_handle",), context={"x_handle": "PachanoDesign"})
    assert not result.all_valid
    assert "x_handle" in result.invalid
    assert result.block_action  # Tier 3 must NOT proceed
```

**Cómo correr:** `.venv/bin/python -m pytest tests/test_success_condition.py -v`

Si pasamos este test, demostramos que:
1. Registry rechaza Tier 3 sin success_condition
2. tool_ok=True con estado externo parcial NO pasa
3. forbidden_reasons bloquean éxito
4. memoria stale (PachanoDesign) bloquea Tier 3

---

## 7. Roll-out plan

| Fase | Cuándo | Riesgo |
|---|---|---|
| **F1**: agregar dataclasses + ToolRegistry validation gates (rechazo) — pero NO obligatorio en runtime, solo warn | Hoy/mañana | Bajo — solo introduce types nuevos, opcional |
| **F2**: validate_success_condition() callable + tests passing | F1+1 día | Bajo — opt-in por tool |
| **F3**: HeyGenDeliver + LinkedIn Publish + X publish gain success_condition obligatorio | F2+1 día | Medio — riesgo de regresión si Las phrases no se actualizan |
| **F4**: registry hace gate hard: tier>=2 sin success_condition NO se registra | F3+3 días | Medio — todos los tools antiguos deben actualizarse o quedan deshabilitados |
| **F5**: memory_revalidation + preflight obligatorio tier 3 | F4+3 días | Bajo si los validators son simples (CDP GET) |

---

## 8. Lo que ESTO previene exactamente

| Falla 2026-05-26 | Cubierto por |
|---|---|
| X thread: `tool.ok=True` con 1/8 tweets posted | F3 — `expected_phrases` de 8 cierres deben aparecer |
| `@PachanoDesign` stale en memoria → publish a cuenta inexistente | F5 — `validate_x_handle` corre antes de publish y BLOQUEA si accountExists=False |
| LinkedIn Quill editor selector falló (4 retries antes de probe) | F5 — preflight `dom_selector_present` exige el editor visible antes del fill |
| Múltiples `usable_reply_unverified` históricos | F2 → F4 — el verifier ya no marca passed sin evidence |

---

## 9. Pendiente decisión tuya

1. **¿Apruebas el plan tal cual?** Si sí, ejecuto F1+F2+test en este turno (escribir dataclasses + validators + test passing).
2. **¿Cambiamos el schema?** Si querés campos distintos o nombres distintos, ajusto antes de tocar código.
3. **¿F1-F2 hoy, F3-F5 mañana?** Mi pick: sí, riesgo bajo y la value es inmediata para los próximos publish que dispares.

**Checkpoint:** plan completo entregado en `docs/audit_success_condition_plan.md`. Schema `success_condition` + `preflight` + memory revalidation diseñados a partir de las 2 fallas reales de hoy (X thread + handle stale). Diff de 4 archivos identificados con line refs exactos. Test mínimo escrito que demuestra `tool_ok=True` NO basta (codifica el incidente X thread como regression). Cero código tocado todavía. Pendiente: tu OK para ejecutar F1+F2.
