"""P0-A: behavior_audit extraction must not silently overwrite outputs.

Two extractor processes ran concurrently on 2026-05-23 and one's
`behavior_cases_sample.jsonl` overwrote the other's. The extractor now
uses per-run identifiers and refuses to overwrite the canonical paths.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import unittest
from pathlib import Path

from claw_v2.behavior_audit_io import (
    build_frontmatter,
    build_output_paths,
    generate_run_id,
    write_exclusive,
)


class BehaviorAuditOutputSafetyTests(unittest.TestCase):
    def test_generate_run_id_has_epoch_and_random_components(self) -> None:
        run_id = generate_run_id()
        # epoch seconds dash 8 hex chars
        self.assertRegex(run_id, r"^\d{10}-[0-9a-f]{8}$")
        self.assertNotEqual(generate_run_id(), generate_run_id())

    def test_build_output_paths_uses_run_id_in_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = build_output_paths(Path(tmp), "run-A")
            self.assertEqual(paths["canonical_jsonl"].name, "behavior_cases_sample.jsonl")
            self.assertEqual(paths["canonical_md"].name, "BEHAVIOR_AUDIT_REPORT.md")
            self.assertIn("run-A", paths["run_jsonl"].name)
            self.assertIn("run-A", paths["run_md"].name)

    def test_write_exclusive_refuses_to_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "out.txt"
            self.assertTrue(write_exclusive(p, "first"))
            self.assertFalse(write_exclusive(p, "second"))
            self.assertEqual(p.read_text(encoding="utf-8"), "first")

    def test_two_simultaneous_runs_preserve_both_artifacts(self) -> None:
        """Closes the 2026-05-23 regression: behavior_cases_sample.jsonl was
        overwritten when two extractor processes ran in the same window."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            paths_a = build_output_paths(out_dir, "runA-aaaaaaaa")
            paths_b = build_output_paths(out_dir, "runB-bbbbbbbb")

            errors: list[BaseException] = []

            def _write(paths: dict[str, Path], label: str) -> None:
                try:
                    paths["run_jsonl"].write_text(f"{label} jsonl\n", encoding="utf-8")
                    paths["run_md"].write_text(f"# {label}\n", encoding="utf-8")
                except BaseException as exc:  # pragma: no cover
                    errors.append(exc)

            t_a = threading.Thread(target=_write, args=(paths_a, "A"))
            t_b = threading.Thread(target=_write, args=(paths_b, "B"))
            t_a.start()
            t_b.start()
            t_a.join()
            t_b.join()

            self.assertEqual(errors, [])
            self.assertEqual(paths_a["run_jsonl"].read_text(encoding="utf-8"), "A jsonl\n")
            self.assertEqual(paths_b["run_jsonl"].read_text(encoding="utf-8"), "B jsonl\n")
            self.assertEqual(paths_a["run_md"].read_text(encoding="utf-8"), "# A\n")
            self.assertEqual(paths_b["run_md"].read_text(encoding="utf-8"), "# B\n")

    def test_concurrent_canonical_writes_winner_takes_all_no_loss(self) -> None:
        """Even if both runs try to claim the canonical path, only the
        first one wins and the second's content lands in its run file."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            canonical = out_dir / "BEHAVIOR_AUDIT_REPORT.md"

            results: dict[str, bool] = {}

            def _try(label: str) -> None:
                results[label] = write_exclusive(canonical, f"# {label}\n")

            t_a = threading.Thread(target=_try, args=("A",))
            t_b = threading.Thread(target=_try, args=("B",))
            t_a.start()
            t_b.start()
            t_a.join()
            t_b.join()

            wins = [k for k, ok in results.items() if ok]
            self.assertEqual(len(wins), 1, "exactly one writer must own the canonical")
            winner = wins[0]
            self.assertEqual(canonical.read_text(encoding="utf-8"), f"# {winner}\n")

    def test_frontmatter_includes_required_fields(self) -> None:
        fm = build_frontmatter(
            run_id="run-x",
            generated_by="reports/behavior_audit/extract_behavior_audit.py",
            source="/Users/hector/Projects/Dr.-strange/reports/behavior_audit/extract_behavior_audit.py",
            started_at=1716100000.0,
            completed_at=1716100100.0,
            canonical=True,
            input_db="data/claw.db",
            sample_size=524,
        )
        # Required fields, in order, between --- markers.
        self.assertTrue(fm.startswith("---\n"))
        self.assertIn("---\n\n", fm)
        for key in (
            "run_id",
            "generated_by",
            "source",
            "started_at",
            "completed_at",
            "canonical",
            "input_db",
            "sample_size",
        ):
            self.assertRegex(fm, rf"\n{key}: ")
        self.assertIn("run_id: run-x", fm)
        self.assertIn("canonical: true", fm)
        self.assertIn("sample_size: 524", fm)
        # Timestamps must be ISO-8601 with timezone.
        self.assertRegex(fm, r"started_at: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
