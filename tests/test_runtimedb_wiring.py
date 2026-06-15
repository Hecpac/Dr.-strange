"""F1.1a1: production wiring of the single RuntimeDb into the runtime stores.

Asserts the production path is fully RuntimeDb-wired (the 5 build_runtime core
stores + capability_grants via the HeyGen tool path), that no production
construction falls back to ``runtime_db=None``, that property_graph is not
production-constructed (dormant), and that the ``runtime_db=None`` back-compat
seam still works for legacy/unit construction.
"""

from __future__ import annotations

import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.adapters.base import LLMRequest
from claw_v2.capability_grants import CapabilityGrantStore
from claw_v2.heygen_readonly import HeyGenReadOnlyAdapter
from claw_v2.main import build_runtime
from claw_v2.memory import MemoryStore
from claw_v2.sqlite_runtime import RuntimeDb, _RuntimeConnHandle, _registry_key, _WAL_HEAL_REGISTRY
from claw_v2.types import LLMResponse

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_STORE_CLASSES = (
    "MemoryStore",
    "ObserveStream",
    "TaskLedger",
    "JobService",
    "OrchestrationStore",
)


def _fake(req: LLMRequest) -> LLMResponse:
    return LLMResponse(content="ok", lane=req.lane, provider="anthropic", model=req.model)


def _build_runtime(tmpdir: str):
    env = {
        k: str(Path(tmpdir) / v)
        for k, v in {
            "DB_PATH": "data/claw.db",
            "WORKSPACE_ROOT": "ws",
            "AGENT_STATE_ROOT": "agents",
            "EVAL_ARTIFACTS_ROOT": "evals",
            "APPROVALS_ROOT": "appr",
            "TELEMETRY_ROOT": "tele",
            "PIPELINE_STATE_ROOT": "pipe",
        }.items()
    }
    env["TELEGRAM_ALLOWED_USER_ID"] = "123"
    with patch.dict(os.environ, env, clear=False):
        return build_runtime(anthropic_executor=_fake)


class BuildRuntimeIdentityTests(unittest.TestCase):
    def test_five_core_stores_share_one_runtimedb_lock_and_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rt = _build_runtime(tmpdir)
            stores = {
                "memory": rt.memory,
                "observe": rt.observe,
                "task_ledger": rt.task_ledger,
                "job_service": rt.job_service,
                "orchestration": rt.coordinator.orchestration_store,
            }
            shared = rt.memory._db
            self.assertIsInstance(shared, RuntimeDb)
            for name, store in stores.items():
                self.assertIs(
                    getattr(store, "_db", None), shared, f"{name} not on the one RuntimeDb"
                )
                self.assertIs(store._lock, shared.lock, f"{name} not on the shared lock")
                # No store caches a raw sqlite3.Connection — only the dynamic handle.
                self.assertIsInstance(store._conn, _RuntimeConnHandle, f"{name} caches a raw conn")

    def test_tool_registry_carries_the_shared_runtimedb(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rt = _build_runtime(tmpdir)
            self.assertIs(rt.tool_registry.runtime_db, rt.memory._db)


class HeyGenCapabilityGrantsWiringTests(unittest.TestCase):
    def test_capability_grants_with_runtime_db_shares_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rt_db = RuntimeDb(Path(tmpdir) / "claw.db")
            self.addCleanup(rt_db.close)
            store = CapabilityGrantStore(rt_db.db_path, runtime_db=rt_db)
            self.assertIs(store._db, rt_db)
            self.assertIsInstance(store._conn, _RuntimeConnHandle)
            # No second heal registration: only the RuntimeDb's own handle.
            handles = _WAL_HEAL_REGISTRY.get(_registry_key(rt_db.db_path), [])
            self.assertEqual(sum(1 for h in handles if h.alive), 1)

    def test_heygen_adapter_threads_runtime_db_into_lazy_capability_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rt_db = RuntimeDb(Path(tmpdir) / "claw.db")
            self.addCleanup(rt_db.close)
            captured: dict[str, object] = {}
            import claw_v2.capability_grants as cg

            real_cls = cg.CapabilityGrantStore

            def spy(*args, **kwargs):
                captured["runtime_db"] = kwargs.get("runtime_db")
                return real_cls(*args, **kwargs)

            adapter = HeyGenReadOnlyAdapter(
                workspace_root=tmpdir,
                db_path=rt_db.db_path,
                runtime_db=rt_db,
                approval_store=None,
            )
            with patch.object(cg, "CapabilityGrantStore", side_effect=spy):
                adapter._active_approval_fingerprint()  # builds the lazy store
            self.assertIs(captured.get("runtime_db"), rt_db)


class ProductionNoFallbackTripwires(unittest.TestCase):
    @staticmethod
    def _store_calls_in_main() -> list[ast.Call]:
        tree = ast.parse((REPO_ROOT / "claw_v2" / "main.py").read_text(encoding="utf-8"))
        return [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in CORE_STORE_CLASSES
        ]

    def test_main_constructs_core_stores_with_a_non_none_runtime_db(self) -> None:
        offenders: list[str] = []
        for call in self._store_calls_in_main():
            kw = next((k for k in call.keywords if k.arg == "runtime_db"), None)
            if kw is None:
                offenders.append(f"{call.func.id}:{call.lineno} missing runtime_db=")
            elif isinstance(kw.value, ast.Constant) and kw.value.value is None:
                offenders.append(f"{call.func.id}:{call.lineno} runtime_db=None")
        self.assertEqual(
            offenders,
            [],
            f"production store construction not RuntimeDb-wired: {offenders}",
        )

    def test_property_graph_is_not_production_constructed(self) -> None:
        # property_graph (PropertyGraphProjection) is dormant in production. If a
        # production constructor is added, it MUST be wired with RuntimeDb and have
        # its read/write gaps wrapped first (see F1.1a1) — this tripwire fails to
        # force that.
        offenders: list[str] = []
        for path in sorted((REPO_ROOT / "claw_v2").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "PropertyGraphProjection"
                ):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
        self.assertEqual(
            offenders,
            [],
            "PropertyGraphProjection is now constructed in production code; wire it "
            f"with RuntimeDb (runtime_db=) before it touches claw.db: {offenders}",
        )


class OptionalInjectionBackCompatTests(unittest.TestCase):
    def test_runtime_db_none_keeps_legacy_own_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "claw.db")  # runtime_db=None (legacy)
            self.assertIsNone(store._db)
            # Legacy path owns a real connection, not the dynamic handle.
            self.assertNotIsInstance(store._conn, _RuntimeConnHandle)


if __name__ == "__main__":
    unittest.main()
