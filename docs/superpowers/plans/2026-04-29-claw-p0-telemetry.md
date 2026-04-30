# Claw P0 — Goal Contract + Evidence Ledger + Typed Action Events

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implementar los primitivos de P0 del Claw Evolution Plan: escritura JSONL append-only, Goal Contract, Evidence Ledger y Typed Action Events.

**Architecture:** Cuatro módulos nuevos (`telemetry`, `goal_contract`, `evidence_ledger`, `action_events`) construidos sobre los módulos existentes `redaction.py` y `observe.py`. Cada módulo persiste en `~/.claw/telemetry/<file>.jsonl` con redaction y `schema_version`. No se modifican rutas de ejecución existentes — solo se añaden primitivos listos para ser llamados.

**Tech Stack:** Python 3.13, stdlib (`uuid`, `json`, `threading`, `datetime`), `claw_v2.redaction` (ya existe), `claw_v2.observe.ObserveStream` (ya existe), `unittest` + `tempfile`.

---

## File Map

| Acción | Archivo | Responsabilidad |
|--------|---------|-----------------|
| Create | `claw_v2/telemetry.py` | JSONL writer thread-safe, `generate_id`, `now_iso`, `read_jsonl` |
| Create | `claw_v2/goal_contract.py` | `GoalContract` dataclass + `create_goal` + `update_goal` + `load_goals` |
| Create | `claw_v2/evidence_ledger.py` | `Claim`/`EvidenceRef` dataclasses + `record_claim` + `load_claims` |
| Create | `claw_v2/action_events.py` | `ActionEvent`/`ProposedAction`/`ActionResult` dataclasses + `emit_event` |
| Modify | `claw_v2/config.py` | Añadir campo `telemetry_root: Path` + default + `ensure_directories` |
| Create | `tests/test_telemetry.py` | Tests para JSONL writer, IDs, redaction, read_jsonl |
| Create | `tests/test_goal_contract.py` | Tests para create/update/load + persistencia |
| Create | `tests/test_evidence_ledger.py` | Tests para record_claim, validación verified, load_claims |
| Create | `tests/test_action_events.py` | Tests para emit_event, schema_version, integración observe |

---

## Task 1: Telemetry Foundation

**Files:**
- Create: `claw_v2/telemetry.py`
- Modify: `claw_v2/config.py` (añadir `telemetry_root`)
- Test: `tests/test_telemetry.py`

- [ ] **Step 1.1: Write failing tests for telemetry**

```python
# tests/test_telemetry.py
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from claw_v2.telemetry import append_jsonl, generate_id, now_iso, read_jsonl


class GenerateIdTests(unittest.TestCase):
    def test_prefix_is_included(self) -> None:
        id_ = generate_id("g")
        self.assertTrue(id_.startswith("g_"), id_)

    def test_ids_are_unique(self) -> None:
        ids = {generate_id("e") for _ in range(100)}
        self.assertEqual(len(ids), 100)

    def test_id_contains_only_safe_chars(self) -> None:
        import re
        id_ = generate_id("c")
        self.assertRegex(id_, r"^[a-z]_[0-9a-f]+$")


class NowIsoTests(unittest.TestCase):
    def test_returns_iso_8601_string(self) -> None:
        from datetime import datetime, timezone
        ts = now_iso()
        parsed = datetime.fromisoformat(ts)
        self.assertIsNotNone(parsed.tzinfo)

    def test_no_microseconds(self) -> None:
        ts = now_iso()
        self.assertNotIn(".", ts.split("T")[1])


class AppendJsonlTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_file_and_appends_line(self) -> None:
        path = self.root / "test.jsonl"
        append_jsonl(path, {"key": "value"})
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), {"key": "value"})

    def test_multiple_appends_produce_multiple_lines(self) -> None:
        path = self.root / "test.jsonl"
        append_jsonl(path, {"n": 1})
        append_jsonl(path, {"n": 2})
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[1])["n"], 2)

    def test_creates_parent_dirs(self) -> None:
        path = self.root / "sub" / "deep" / "out.jsonl"
        append_jsonl(path, {"x": 1})
        self.assertTrue(path.exists())

    def test_redacts_api_key_in_record(self) -> None:
        path = self.root / "test.jsonl"
        append_jsonl(path, {"api_key": "sk-abc123def456ghi789jkl"})
        raw = path.read_text()
        self.assertNotIn("sk-abc123", raw)
        self.assertIn("[REDACTED]", raw)

    def test_thread_safe_concurrent_writes(self) -> None:
        path = self.root / "concurrent.jsonl"
        errors: list[Exception] = []

        def write(n: int) -> None:
            try:
                append_jsonl(path, {"n": n})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 20)


class ReadJsonlTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_returns_empty_list_for_missing_file(self) -> None:
        result = read_jsonl(self.root / "missing.jsonl")
        self.assertEqual(result, [])

    def test_reads_back_appended_records(self) -> None:
        path = self.root / "data.jsonl"
        append_jsonl(path, {"a": 1})
        append_jsonl(path, {"b": 2})
        records = read_jsonl(path)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["a"], 1)
        self.assertEqual(records[1]["b"], 2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 1.2: Run tests — verify they fail**

```bash
cd /Users/hector/Projects/Dr.-strange
python -m pytest tests/test_telemetry.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'claw_v2.telemetry'`

- [ ] **Step 1.3: Create `claw_v2/telemetry.py`**

```python
# claw_v2/telemetry.py
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .redaction import redact_sensitive

_lock = threading.Lock()


def generate_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = redact_sensitive(record)
    line = json.dumps(safe, ensure_ascii=False)
    with _lock:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if raw:
                records.append(json.loads(raw))
    return records
```

- [ ] **Step 1.4: Run tests — verify they pass**

```bash
python -m pytest tests/test_telemetry.py -v
```

Expected: All tests PASS.

- [ ] **Step 1.5: Add `telemetry_root` to `AppConfig`**

En `claw_v2/config.py`, localizar la línea con `pipeline_state_root: Path` (≈línea 295) y añadir el campo justo después:

```python
    pipeline_state_root: Path
    telemetry_root: Path   # ← añadir esta línea
    runtime_config_path: Path | None
```

Luego en `from_env()` (≈línea 393), después de `pipeline_state_root=...`:

```python
            pipeline_state_root=Path(os.getenv("PIPELINE_STATE_ROOT", str(home / ".claw" / "pipeline"))),
            telemetry_root=Path(os.getenv("TELEMETRY_ROOT", str(home / ".claw" / "telemetry"))),  # ← añadir
            runtime_config_path=runtime_config_path,
```

Y en `ensure_directories()` (≈línea 455), añadir:

```python
        self.pipeline_state_root.mkdir(parents=True, exist_ok=True)
        self.telemetry_root.mkdir(parents=True, exist_ok=True)  # ← añadir
```

- [ ] **Step 1.6: Verify existing config tests still pass**

```bash
python -m pytest tests/test_config.py -v
```

Expected: All tests PASS (ningún test depende de la ausencia de `telemetry_root`).

- [ ] **Step 1.7: Commit**

```bash
git add claw_v2/telemetry.py tests/test_telemetry.py claw_v2/config.py
git commit -m "feat(p0): add telemetry JSONL writer and config.telemetry_root"
```

---

## Task 2: Goal Contract

**Files:**
- Create: `claw_v2/goal_contract.py`
- Test: `tests/test_goal_contract.py`

- [ ] **Step 2.1: Write failing tests for Goal Contract**

```python
# tests/test_goal_contract.py
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from claw_v2.goal_contract import (
    SCHEMA_VERSION,
    GoalContract,
    create_goal,
    load_goals,
    update_goal,
)


class CreateGoalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_returns_goal_contract_with_goal_id(self) -> None:
        goal = create_goal(
            objective="Deploy TIC Insurance to Vercel",
            risk_profile="tier_2_5",
            telemetry_root=self.root,
        )
        self.assertTrue(goal.goal_id.startswith("g_"))

    def test_schema_version_is_set(self) -> None:
        goal = create_goal(
            objective="Test goal",
            risk_profile="tier_1",
            telemetry_root=self.root,
        )
        self.assertEqual(goal.schema_version, SCHEMA_VERSION)

    def test_persists_to_goals_jsonl(self) -> None:
        create_goal(
            objective="Persist me",
            risk_profile="tier_1",
            telemetry_root=self.root,
        )
        path = self.root / "goals.jsonl"
        self.assertTrue(path.exists())
        record = json.loads(path.read_text().strip())
        self.assertEqual(record["event_type"], "goal_initialized")
        self.assertEqual(record["objective"], "Persist me")

    def test_constraints_and_assumptions_stored(self) -> None:
        goal = create_goal(
            objective="Deploy",
            risk_profile="tier_2",
            telemetry_root=self.root,
            constraints=["no force-push"],
            assumptions=["Vercel already connected"],
        )
        self.assertEqual(goal.constraints, ["no force-push"])
        self.assertEqual(goal.assumptions, ["Vercel already connected"])

    def test_created_at_and_updated_at_are_equal_on_creation(self) -> None:
        goal = create_goal(
            objective="Fresh goal",
            risk_profile="tier_1",
            telemetry_root=self.root,
        )
        self.assertEqual(goal.created_at, goal.updated_at)

    def test_parent_goal_id_defaults_to_none(self) -> None:
        goal = create_goal(
            objective="Top-level goal",
            risk_profile="tier_1",
            telemetry_root=self.root,
        )
        self.assertIsNone(goal.parent_goal_id)

    def test_anchor_source_defaults_to_manual(self) -> None:
        goal = create_goal(
            objective="Manual goal",
            risk_profile="tier_1",
            telemetry_root=self.root,
        )
        self.assertEqual(goal.anchor_source, "manual")


class UpdateGoalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.goal = create_goal(
            objective="Original",
            risk_profile="tier_1",
            telemetry_root=self.root,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_update_changes_objective(self) -> None:
        update_goal(self.goal, telemetry_root=self.root, objective="Updated")
        self.assertEqual(self.goal.objective, "Updated")

    def test_update_refreshes_updated_at(self) -> None:
        original_updated = self.goal.updated_at
        import time; time.sleep(1.1)
        update_goal(self.goal, telemetry_root=self.root, objective="Changed")
        self.assertNotEqual(self.goal.updated_at, original_updated)

    def test_update_appends_goal_updated_event(self) -> None:
        update_goal(self.goal, telemetry_root=self.root, objective="Changed")
        path = self.root / "goals.jsonl"
        lines = path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)
        second = json.loads(lines[1])
        self.assertEqual(second["event_type"], "goal_updated")
        self.assertEqual(second["objective"], "Changed")

    def test_update_rejects_unknown_field(self) -> None:
        with self.assertRaises(ValueError):
            update_goal(self.goal, telemetry_root=self.root, nonexistent_field="x")


class LoadGoalsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_returns_empty_list_when_no_file(self) -> None:
        self.assertEqual(load_goals(self.root), [])

    def test_returns_goals_in_order(self) -> None:
        g1 = create_goal(objective="A", risk_profile="tier_1", telemetry_root=self.root)
        g2 = create_goal(objective="B", risk_profile="tier_2", telemetry_root=self.root)
        goals = load_goals(self.root)
        self.assertEqual(goals[0].goal_id, g1.goal_id)
        self.assertEqual(goals[1].goal_id, g2.goal_id)

    def test_roundtrip_preserves_all_fields(self) -> None:
        original = create_goal(
            objective="Roundtrip",
            risk_profile="tier_3",
            telemetry_root=self.root,
            constraints=["c1"],
            assumptions=["a1"],
            success_criteria=["done"],
        )
        loaded = load_goals(self.root)[0]
        self.assertEqual(loaded.goal_id, original.goal_id)
        self.assertEqual(loaded.objective, original.objective)
        self.assertEqual(loaded.constraints, ["c1"])
        self.assertEqual(loaded.assumptions, ["a1"])
        self.assertEqual(loaded.success_criteria, ["done"])


class GoalContractToDictTests(unittest.TestCase):
    def test_to_dict_contains_schema_version(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        goal = create_goal(
            objective="Dict test",
            risk_profile="tier_1",
            telemetry_root=Path(tmp.name),
        )
        tmp.cleanup()
        d = goal.to_dict()
        self.assertIn("schema_version", d)
        self.assertEqual(d["schema_version"], SCHEMA_VERSION)

    def test_from_dict_roundtrip(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        original = create_goal(
            objective="Roundtrip dict",
            risk_profile="tier_2",
            telemetry_root=Path(tmp.name),
        )
        tmp.cleanup()
        restored = GoalContract.from_dict(original.to_dict())
        self.assertEqual(restored.goal_id, original.goal_id)
        self.assertEqual(restored.risk_profile, original.risk_profile)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2.2: Run tests — verify they fail**

```bash
python -m pytest tests/test_goal_contract.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'claw_v2.goal_contract'`

- [ ] **Step 2.3: Create `claw_v2/goal_contract.py`**

```python
# claw_v2/goal_contract.py
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .telemetry import append_jsonl, generate_id, now_iso, read_jsonl

SCHEMA_VERSION = "goal_contract.v1"

GoalRiskProfile = Literal["tier_1", "tier_2", "tier_2_5", "tier_3"]

_UPDATABLE_FIELDS = frozenset({
    "objective",
    "risk_profile",
    "constraints",
    "assumptions",
    "allowed_actions",
    "disallowed_actions",
    "success_criteria",
    "stop_conditions",
    "anchor_source",
    "parent_goal_id",
})


@dataclass(slots=True)
class GoalContract:
    schema_version: str
    goal_id: str
    objective: str
    risk_profile: str
    constraints: list[str]
    assumptions: list[str]
    allowed_actions: list[str]
    disallowed_actions: list[str]
    success_criteria: list[str]
    stop_conditions: list[str]
    anchor_source: str
    parent_goal_id: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "goal_id": self.goal_id,
            "objective": self.objective,
            "risk_profile": self.risk_profile,
            "constraints": self.constraints,
            "assumptions": self.assumptions,
            "allowed_actions": self.allowed_actions,
            "disallowed_actions": self.disallowed_actions,
            "success_criteria": self.success_criteria,
            "stop_conditions": self.stop_conditions,
            "anchor_source": self.anchor_source,
            "parent_goal_id": self.parent_goal_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoalContract:
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            goal_id=data["goal_id"],
            objective=data["objective"],
            risk_profile=data["risk_profile"],
            constraints=list(data.get("constraints") or []),
            assumptions=list(data.get("assumptions") or []),
            allowed_actions=list(data.get("allowed_actions") or []),
            disallowed_actions=list(data.get("disallowed_actions") or []),
            success_criteria=list(data.get("success_criteria") or []),
            stop_conditions=list(data.get("stop_conditions") or []),
            anchor_source=data.get("anchor_source", "manual"),
            parent_goal_id=data.get("parent_goal_id"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


def create_goal(
    *,
    objective: str,
    risk_profile: str,
    telemetry_root: Path,
    constraints: list[str] | None = None,
    assumptions: list[str] | None = None,
    allowed_actions: list[str] | None = None,
    disallowed_actions: list[str] | None = None,
    success_criteria: list[str] | None = None,
    stop_conditions: list[str] | None = None,
    anchor_source: str = "manual",
    parent_goal_id: str | None = None,
) -> GoalContract:
    ts = now_iso()
    goal = GoalContract(
        schema_version=SCHEMA_VERSION,
        goal_id=generate_id("g"),
        objective=objective,
        risk_profile=risk_profile,
        constraints=list(constraints or []),
        assumptions=list(assumptions or []),
        allowed_actions=list(allowed_actions or []),
        disallowed_actions=list(disallowed_actions or []),
        success_criteria=list(success_criteria or []),
        stop_conditions=list(stop_conditions or []),
        anchor_source=anchor_source,
        parent_goal_id=parent_goal_id,
        created_at=ts,
        updated_at=ts,
    )
    record = {"event_type": "goal_initialized", **goal.to_dict()}
    append_jsonl(telemetry_root / "goals.jsonl", record)
    return goal


def update_goal(
    goal: GoalContract,
    *,
    telemetry_root: Path,
    **kwargs: Any,
) -> GoalContract:
    for key in kwargs:
        if key not in _UPDATABLE_FIELDS:
            raise ValueError(f"Cannot update field: {key!r}")
    for key, value in kwargs.items():
        object.__setattr__(goal, key, value)
    object.__setattr__(goal, "updated_at", now_iso())
    record = {"event_type": "goal_updated", **goal.to_dict()}
    append_jsonl(telemetry_root / "goals.jsonl", record)
    return goal


def load_goals(telemetry_root: Path) -> list[GoalContract]:
    return [GoalContract.from_dict(r) for r in read_jsonl(telemetry_root / "goals.jsonl")]
```

- [ ] **Step 2.4: Run tests — verify they pass**

```bash
python -m pytest tests/test_goal_contract.py -v
```

Expected: All tests PASS. El test `test_update_refreshes_updated_at` puede tardar 1s por el `time.sleep(1.1)`.

- [ ] **Step 2.5: Commit**

```bash
git add claw_v2/goal_contract.py tests/test_goal_contract.py
git commit -m "feat(p0): add GoalContract — create, update, persist, load"
```

---

## Task 3: Evidence Ledger

**Files:**
- Create: `claw_v2/evidence_ledger.py`
- Test: `tests/test_evidence_ledger.py`

- [ ] **Step 3.1: Write failing tests for Evidence Ledger**

```python
# tests/test_evidence_ledger.py
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from claw_v2.evidence_ledger import (
    SCHEMA_VERSION,
    Claim,
    EvidenceRef,
    load_claims,
    record_claim,
)


class RecordClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.goal_id = "g_test000000000000000001"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_returns_claim_with_claim_id(self) -> None:
        claim = record_claim(
            goal_id=self.goal_id,
            claim_text="El daemon está corriendo.",
            claim_type="inference",
            telemetry_root=self.root,
        )
        self.assertTrue(claim.claim_id.startswith("c_"))

    def test_schema_version_is_set(self) -> None:
        claim = record_claim(
            goal_id=self.goal_id,
            claim_text="Fact claim",
            claim_type="assumption",
            telemetry_root=self.root,
        )
        self.assertEqual(claim.schema_version, SCHEMA_VERSION)

    def test_persists_to_claims_jsonl(self) -> None:
        record_claim(
            goal_id=self.goal_id,
            claim_text="Port 8765 is listening.",
            claim_type="fact",
            telemetry_root=self.root,
            evidence_refs=[
                EvidenceRef(kind="tool_call", ref="lsof:8765→LISTEN", captured_at="2026-04-29T00:00:00+00:00")
            ],
            verification_status="verified",
        )
        path = self.root / "claims.jsonl"
        self.assertTrue(path.exists())
        record = json.loads(path.read_text().strip())
        self.assertEqual(record["event_type"], "claim_recorded")
        self.assertEqual(record["claim_text"], "Port 8765 is listening.")

    def test_verified_claim_without_evidence_raises(self) -> None:
        with self.assertRaises(ValueError):
            record_claim(
                goal_id=self.goal_id,
                claim_text="Ungrounded verified claim.",
                claim_type="fact",
                telemetry_root=self.root,
                verification_status="verified",
            )

    def test_inference_without_evidence_is_allowed(self) -> None:
        claim = record_claim(
            goal_id=self.goal_id,
            claim_text="Probably working.",
            claim_type="inference",
            telemetry_root=self.root,
        )
        self.assertEqual(claim.verification_status, "unverified")

    def test_confidence_is_stored(self) -> None:
        claim = record_claim(
            goal_id=self.goal_id,
            claim_text="Highly confident fact.",
            claim_type="assumption",
            telemetry_root=self.root,
            confidence=0.95,
        )
        self.assertAlmostEqual(claim.confidence, 0.95)

    def test_depends_on_is_stored(self) -> None:
        claim = record_claim(
            goal_id=self.goal_id,
            claim_text="Derived claim.",
            claim_type="decision",
            telemetry_root=self.root,
            depends_on=["c_abc", "c_xyz"],
        )
        self.assertEqual(claim.depends_on, ["c_abc", "c_xyz"])


class EvidenceRefTests(unittest.TestCase):
    def test_evidence_ref_fields(self) -> None:
        ref = EvidenceRef(
            kind="tool_call",
            ref="launchctl list → PID=32786",
            captured_at="2026-04-29T00:00:00+00:00",
        )
        self.assertEqual(ref.kind, "tool_call")
        self.assertEqual(ref.ref, "launchctl list → PID=32786")

    def test_evidence_ref_kinds_accepted(self) -> None:
        for kind in ("tool_call", "file_read", "log_line", "memory_entry", "user_message", "external_api"):
            ref = EvidenceRef(kind=kind, ref="x", captured_at="2026-04-29T00:00:00+00:00")
            self.assertEqual(ref.kind, kind)


class LoadClaimsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_returns_empty_list_when_no_file(self) -> None:
        self.assertEqual(load_claims(self.root), [])

    def test_returns_all_claims(self) -> None:
        goal_id = "g_test_load"
        record_claim(
            goal_id=goal_id,
            claim_text="Claim 1",
            claim_type="assumption",
            telemetry_root=self.root,
        )
        record_claim(
            goal_id=goal_id,
            claim_text="Claim 2",
            claim_type="inference",
            telemetry_root=self.root,
        )
        claims = load_claims(self.root)
        self.assertEqual(len(claims), 2)

    def test_filters_by_goal_id(self) -> None:
        record_claim(
            goal_id="g_a",
            claim_text="For A",
            claim_type="assumption",
            telemetry_root=self.root,
        )
        record_claim(
            goal_id="g_b",
            claim_text="For B",
            claim_type="assumption",
            telemetry_root=self.root,
        )
        claims_a = load_claims(self.root, goal_id="g_a")
        self.assertEqual(len(claims_a), 1)
        self.assertEqual(claims_a[0].claim_text, "For A")

    def test_roundtrip_preserves_evidence_refs(self) -> None:
        ref = EvidenceRef(kind="file_read", ref="/etc/hosts:L1", captured_at="2026-04-29T00:00:00+00:00")
        original = record_claim(
            goal_id="g_rt",
            claim_text="Hosts entry exists.",
            claim_type="fact",
            telemetry_root=self.root,
            evidence_refs=[ref],
            verification_status="verified",
        )
        loaded = load_claims(self.root)[0]
        self.assertEqual(len(loaded.evidence_refs), 1)
        self.assertEqual(loaded.evidence_refs[0].kind, "file_read")
        self.assertEqual(loaded.evidence_refs[0].ref, original.evidence_refs[0].ref)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3.2: Run tests — verify they fail**

```bash
python -m pytest tests/test_evidence_ledger.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'claw_v2.evidence_ledger'`

- [ ] **Step 3.3: Create `claw_v2/evidence_ledger.py`**

```python
# claw_v2/evidence_ledger.py
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .telemetry import append_jsonl, generate_id, now_iso, read_jsonl

SCHEMA_VERSION = "evidence_ledger.v1"


@dataclass(slots=True)
class EvidenceRef:
    kind: str
    ref: str
    captured_at: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "ref": self.ref, "captured_at": self.captured_at}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceRef:
        return cls(kind=data["kind"], ref=data["ref"], captured_at=data["captured_at"])


@dataclass(slots=True)
class Claim:
    schema_version: str
    claim_id: str
    goal_id: str
    claim_text: str
    claim_type: str
    evidence_refs: list[EvidenceRef]
    verification_status: str
    confidence: float | None
    depends_on: list[str]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "claim_id": self.claim_id,
            "goal_id": self.goal_id,
            "claim_text": self.claim_text,
            "claim_type": self.claim_type,
            "evidence_refs": [e.to_dict() for e in self.evidence_refs],
            "verification_status": self.verification_status,
            "confidence": self.confidence,
            "depends_on": self.depends_on,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Claim:
        refs = [EvidenceRef.from_dict(r) for r in data.get("evidence_refs") or []]
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            claim_id=data["claim_id"],
            goal_id=data["goal_id"],
            claim_text=data["claim_text"],
            claim_type=data["claim_type"],
            evidence_refs=refs,
            verification_status=data.get("verification_status", "unverified"),
            confidence=data.get("confidence"),
            depends_on=list(data.get("depends_on") or []),
            created_at=data.get("created_at", ""),
        )


def record_claim(
    *,
    goal_id: str,
    claim_text: str,
    claim_type: str,
    telemetry_root: Path,
    evidence_refs: list[EvidenceRef] | None = None,
    verification_status: str = "unverified",
    confidence: float | None = None,
    depends_on: list[str] | None = None,
) -> Claim:
    refs = list(evidence_refs or [])
    if verification_status == "verified" and not refs:
        raise ValueError("A 'verified' claim requires at least one evidence_ref.")
    claim = Claim(
        schema_version=SCHEMA_VERSION,
        claim_id=generate_id("c"),
        goal_id=goal_id,
        claim_text=claim_text,
        claim_type=claim_type,
        evidence_refs=refs,
        verification_status=verification_status,
        confidence=confidence,
        depends_on=list(depends_on or []),
        created_at=now_iso(),
    )
    record = {"event_type": "claim_recorded", **claim.to_dict()}
    append_jsonl(telemetry_root / "claims.jsonl", record)
    return claim


def load_claims(
    telemetry_root: Path,
    *,
    goal_id: str | None = None,
) -> list[Claim]:
    claims = [Claim.from_dict(r) for r in read_jsonl(telemetry_root / "claims.jsonl")]
    if goal_id is not None:
        claims = [c for c in claims if c.goal_id == goal_id]
    return claims
```

- [ ] **Step 3.4: Run tests — verify they pass**

```bash
python -m pytest tests/test_evidence_ledger.py -v
```

Expected: All tests PASS.

- [ ] **Step 3.5: Commit**

```bash
git add claw_v2/evidence_ledger.py tests/test_evidence_ledger.py
git commit -m "feat(p0): add EvidenceLedger — Claim, EvidenceRef, record, load"
```

---

## Task 4: Typed Action Events

**Files:**
- Create: `claw_v2/action_events.py`
- Test: `tests/test_action_events.py`

- [ ] **Step 4.1: Write failing tests for Action Events**

```python
# tests/test_action_events.py
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.action_events import (
    SCHEMA_VERSION,
    ActionEvent,
    ActionResult,
    ProposedAction,
    emit_event,
)


class EmitEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_returns_action_event_with_event_id(self) -> None:
        event = emit_event(
            event_type="action_executed",
            goal_id="g_test",
            session_id="tg-574707975",
            telemetry_root=self.root,
        )
        self.assertTrue(event.event_id.startswith("e_"))

    def test_schema_version_is_set(self) -> None:
        event = emit_event(
            event_type="goal_initialized",
            goal_id="g_test",
            session_id="web-1",
            telemetry_root=self.root,
        )
        self.assertEqual(event.schema_version, SCHEMA_VERSION)

    def test_persists_to_events_jsonl(self) -> None:
        emit_event(
            event_type="action_executed",
            goal_id="g_test",
            session_id="tg-1",
            telemetry_root=self.root,
        )
        path = self.root / "events.jsonl"
        self.assertTrue(path.exists())
        record = json.loads(path.read_text().strip())
        self.assertEqual(record["event_type"], "action_executed")
        self.assertEqual(record["goal_id"], "g_test")

    def test_proposed_action_is_stored(self) -> None:
        action = ProposedAction(
            tool="git_push",
            tier="tier_2_5",
            rationale_brief="publish commits to origin/main",
            args_redacted={"remote": "origin", "branch": "main"},
        )
        event = emit_event(
            event_type="action_proposed",
            goal_id="g_x",
            session_id="tg-1",
            telemetry_root=self.root,
            proposed_next_action=action,
        )
        self.assertIsNotNone(event.proposed_next_action)
        self.assertEqual(event.proposed_next_action.tool, "git_push")

    def test_proposed_action_persisted_in_jsonl(self) -> None:
        action = ProposedAction(
            tool="write_file",
            tier="tier_1",
            rationale_brief="write output",
        )
        emit_event(
            event_type="action_executed",
            goal_id="g_y",
            session_id="web-1",
            telemetry_root=self.root,
            proposed_next_action=action,
        )
        record = json.loads((self.root / "events.jsonl").read_text().strip())
        self.assertIn("proposed_next_action", record)
        self.assertEqual(record["proposed_next_action"]["tool"], "write_file")

    def test_result_is_stored(self) -> None:
        result = ActionResult(status="success", output_hash="sha256:abc123")
        event = emit_event(
            event_type="action_executed",
            goal_id="g_z",
            session_id="cron-1",
            telemetry_root=self.root,
            result=result,
        )
        self.assertIsNotNone(event.result)
        self.assertEqual(event.result.status, "success")

    def test_risk_level_defaults_to_low(self) -> None:
        event = emit_event(
            event_type="action_executed",
            goal_id="g_risk",
            session_id="web-1",
            telemetry_root=self.root,
        )
        self.assertEqual(event.risk_level, "low")

    def test_actor_defaults_to_claw(self) -> None:
        event = emit_event(
            event_type="action_executed",
            goal_id="g_actor",
            session_id="web-1",
            telemetry_root=self.root,
        )
        self.assertEqual(event.actor, "claw")

    def test_claims_and_evidence_refs_stored(self) -> None:
        event = emit_event(
            event_type="action_executed",
            goal_id="g_claims",
            session_id="tg-1",
            telemetry_root=self.root,
            claims=["c_abc", "c_def"],
            evidence_refs=["e_prev"],
        )
        self.assertEqual(event.claims, ["c_abc", "c_def"])
        self.assertEqual(event.evidence_refs, ["e_prev"])

    def test_timestamp_in_jsonl(self) -> None:
        emit_event(
            event_type="action_executed",
            goal_id="g_ts",
            session_id="web-1",
            telemetry_root=self.root,
        )
        record = json.loads((self.root / "events.jsonl").read_text().strip())
        self.assertIn("timestamp", record)
        self.assertIn("T", record["timestamp"])

    def test_observe_stream_is_called_when_provided(self) -> None:
        mock_observe = MagicMock()
        emit_event(
            event_type="action_executed",
            goal_id="g_obs",
            session_id="web-1",
            telemetry_root=self.root,
            observe=mock_observe,
        )
        mock_observe.emit.assert_called_once()
        call_args = mock_observe.emit.call_args
        self.assertEqual(call_args[0][0], "action_executed")

    def test_observe_failure_does_not_raise(self) -> None:
        mock_observe = MagicMock()
        mock_observe.emit.side_effect = RuntimeError("observe down")
        # Should not raise
        emit_event(
            event_type="action_executed",
            goal_id="g_obs_fail",
            session_id="web-1",
            telemetry_root=self.root,
            observe=mock_observe,
        )

    def test_multiple_events_produce_multiple_lines(self) -> None:
        for i in range(3):
            emit_event(
                event_type="action_executed",
                goal_id=f"g_{i}",
                session_id="web-1",
                telemetry_root=self.root,
            )
        lines = (self.root / "events.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 3)


class ActionEventToDictTests(unittest.TestCase):
    def test_to_dict_contains_required_keys(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        event = emit_event(
            event_type="risk_escalated",
            goal_id="g_dict",
            session_id="web-1",
            telemetry_root=Path(tmp.name),
            risk_level="high",
        )
        tmp.cleanup()
        d = event.to_dict()
        for key in ("schema_version", "event_id", "event_type", "actor", "goal_id",
                    "session_id", "risk_level", "timestamp", "claims", "evidence_refs"):
            self.assertIn(key, d, f"Missing key: {key}")

    def test_to_dict_omits_none_proposed_action(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        event = emit_event(
            event_type="action_executed",
            goal_id="g_omit",
            session_id="web-1",
            telemetry_root=Path(tmp.name),
        )
        tmp.cleanup()
        d = event.to_dict()
        self.assertNotIn("proposed_next_action", d)

    def test_to_dict_omits_none_result(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        event = emit_event(
            event_type="action_executed",
            goal_id="g_omit2",
            session_id="web-1",
            telemetry_root=Path(tmp.name),
        )
        tmp.cleanup()
        d = event.to_dict()
        self.assertNotIn("result", d)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4.2: Run tests — verify they fail**

```bash
python -m pytest tests/test_action_events.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'claw_v2.action_events'`

- [ ] **Step 4.3: Create `claw_v2/action_events.py`**

```python
# claw_v2/action_events.py
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .telemetry import append_jsonl, generate_id, now_iso

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "action_event.v1"


@dataclass(slots=True)
class ProposedAction:
    tool: str
    tier: str
    rationale_brief: str
    args_redacted: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "args_redacted": self.args_redacted,
            "tier": self.tier,
            "rationale_brief": self.rationale_brief,
        }


@dataclass(slots=True)
class ActionResult:
    status: str
    output_hash: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "output_hash": self.output_hash,
            "error": self.error,
        }


@dataclass(slots=True)
class ActionEvent:
    schema_version: str
    event_id: str
    event_type: str
    actor: str
    goal_id: str
    session_id: str
    risk_level: str
    timestamp: str
    proposed_next_action: ProposedAction | None
    claims: list[str]
    evidence_refs: list[str]
    result: ActionResult | None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "actor": self.actor,
            "goal_id": self.goal_id,
            "session_id": self.session_id,
            "risk_level": self.risk_level,
            "timestamp": self.timestamp,
            "claims": self.claims,
            "evidence_refs": self.evidence_refs,
        }
        if self.proposed_next_action is not None:
            d["proposed_next_action"] = self.proposed_next_action.to_dict()
        if self.result is not None:
            d["result"] = self.result.to_dict()
        return d


def emit_event(
    *,
    event_type: str,
    goal_id: str,
    session_id: str,
    telemetry_root: Path,
    actor: str = "claw",
    risk_level: str = "low",
    proposed_next_action: ProposedAction | None = None,
    claims: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    result: ActionResult | None = None,
    observe: Any | None = None,
) -> ActionEvent:
    event = ActionEvent(
        schema_version=SCHEMA_VERSION,
        event_id=generate_id("e"),
        event_type=event_type,
        actor=actor,
        goal_id=goal_id,
        session_id=session_id,
        risk_level=risk_level,
        timestamp=now_iso(),
        proposed_next_action=proposed_next_action,
        claims=list(claims or []),
        evidence_refs=list(evidence_refs or []),
        result=result,
    )
    append_jsonl(telemetry_root / "events.jsonl", event.to_dict())
    if observe is not None:
        try:
            observe.emit(
                event_type,
                payload={
                    "goal_id": goal_id,
                    "session_id": session_id,
                    "risk_level": risk_level,
                },
            )
        except Exception:
            logger.debug("observe.emit failed for %s", event_type, exc_info=True)
    return event
```

- [ ] **Step 4.4: Run tests — verify they pass**

```bash
python -m pytest tests/test_action_events.py -v
```

Expected: All tests PASS.

- [ ] **Step 4.5: Run full test suite — verify no regressions**

```bash
python -m pytest tests/ -x -q 2>&1 | tail -10
```

Expected: Solo puede fallar el test pre-existente `test_build_runtime_resumes_interrupted_autonomous_tasks`. Todos los demás deben pasar.

- [ ] **Step 4.6: Commit**

```bash
git add claw_v2/action_events.py tests/test_action_events.py
git commit -m "feat(p0): add ActionEvents — emit_event, ProposedAction, ActionResult"
```

---

## Task 5: Track docs/claw-evolution in git

**Files:**
- Modify: `.gitignore`

- [ ] **Step 5.1: Remove claw-evolution from .gitignore**

```bash
grep -n "claw-evolution" /Users/hector/Projects/Dr.-strange/.gitignore
```

Verificar el número de línea y remover esa línea del `.gitignore`.

- [ ] **Step 5.2: Stage and commit the design docs**

```bash
git add docs/claw-evolution/ docs/superpowers/
git commit -m "docs(p0): add claw evolution plan and implementation plan"
```

---

## Self-Review

### Spec coverage

| Req del spec P0 | Tarea |
|-----------------|-------|
| Goal Contract con `schema_version`, `goal_id`, campos requeridos | Task 2 |
| Evidence Ledger con `claim_id`, `evidence_refs`, `verification_status` | Task 3 |
| Typed Action Events, todos los `event_type` del spec | Task 4 |
| Append-only JSONL | Task 1 (`append_jsonl`) |
| Redaction | Task 1 (usa `redact_sensitive` existente) |
| `schema_version` en todos los artefactos | Tasks 2, 3, 4 |
| `~/.claw/telemetry/` como directorio | Tasks 1, config |

### Gaps identificados

- P0 **no conecta** los primitivos al flujo de ejecución del brain/coordinator. Eso es P1 (GDI) — los primitivos solo necesitan existir y estar testeados en P0. ✓ Correcto según el spec.
- Los docs `06-active-recall.md`, `07-far-doubt-flags.md`, `08-storage-and-redaction.md` no están escritos todavía — quedan para sus fases (P3, P5).

### Placeholder scan

Ningún paso contiene TBD, TODO ni "implement later". Todos los pasos de código muestran la implementación completa. ✓

### Type consistency

- `GoalContract.from_dict` / `to_dict` — consistente entre Task 2 step 2.3 y tests en 2.1 ✓
- `Claim.from_dict` / `to_dict` — consistente entre Task 3 step 3.3 y tests en 3.1 ✓
- `ActionEvent.to_dict` — `proposed_next_action` y `result` se omiten si son `None` — testado en Task 4 ✓
- `update_goal` usa `object.__setattr__` porque el dataclass tiene `slots=True` ✓
