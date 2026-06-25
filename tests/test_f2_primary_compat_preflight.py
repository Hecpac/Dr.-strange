from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from claw_v2 import f2_primary_compat_preflight as preflight
from claw_v2.f2_durability_schema import (
    F2_DURABILITY_SCHEMA_VERSION,
    F2_DURABILITY_TABLES,
)
from claw_v2.f2_durability_store import F2DurabilityStore
from claw_v2.sqlite_runtime import RuntimeDb

_EXPECTED_UNIQUE_INDEXES = {
    "ux_phase_checkpoints_task_run_phase_version",
    "ux_external_effect_records_idempotency_key",
    "ux_phase_checkpoint_writes_order",
    "ux_phase_checkpoint_writes_key",
    "ux_phase_recovery_cursors_task_run",
}


class ExpectedSchemaTests(unittest.TestCase):
    def test_expected_schema_has_f2_tables_and_unique_indexes(self) -> None:
        expected = preflight.expected_f2_schema()
        self.assertEqual(expected.schema_version, F2_DURABILITY_SCHEMA_VERSION)
        # All 4 F2 tables present with non-empty column lists.
        self.assertEqual(set(expected.tables), set(F2_DURABILITY_TABLES))
        for table, cols in expected.tables.items():
            self.assertTrue(cols, f"no columns introspected for {table}")
        # The 5 F2 unique indexes are a subset of what was introspected.
        self.assertTrue(
            _EXPECTED_UNIQUE_INDEXES.issubset(set(expected.unique_indexes)),
            f"missing F2 unique indexes: "
            f"{_EXPECTED_UNIQUE_INDEXES - set(expected.unique_indexes)}",
        )
        idem = expected.unique_indexes["ux_external_effect_records_idempotency_key"]
        self.assertTrue(idem.unique)
        self.assertEqual(set(idem.columns), {"idempotency_key"})


if __name__ == "__main__":
    unittest.main()
