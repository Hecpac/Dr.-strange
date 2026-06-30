from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.workspace_traversal import (
    DEFAULT_SKIP_DIRS,
    TraversalPolicy,
    WorkspaceTraversalService,
)


class _StepClock:
    def __init__(self, *, step: float = 0.01) -> None:
        self.current = 0.0
        self.step = step

    def __call__(self) -> float:
        self.current += self.step
        return self.current


class WorkspaceTraversalServiceTests(unittest.TestCase):
    def test_glob_respects_max_matches_and_reports_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for idx in range(10):
                (root / f"file_{idx}.py").write_text("x", encoding="utf-8")
            service = WorkspaceTraversalService(
                root, policy=TraversalPolicy(max_matches=3, max_files=50)
            )

            result = service.glob_files(pattern="**/*.py")

            self.assertEqual(len(result.matches), 3)
            self.assertEqual(result.telemetry.matches_returned, 3)
            self.assertTrue(result.telemetry.truncated)
            self.assertIn("max_matches", result.telemetry.skipped_reasons)

    def test_glob_respects_max_files_before_scanning_entire_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for idx in range(20):
                (root / f"file_{idx}.txt").write_text("x", encoding="utf-8")
            service = WorkspaceTraversalService(
                root, policy=TraversalPolicy(max_files=4, max_matches=100)
            )

            result = service.glob_files(pattern="**/*.txt")

            self.assertLessEqual(result.telemetry.files_scanned, 4)
            self.assertTrue(result.telemetry.truncated)
            self.assertIn("max_files", result.telemetry.skipped_reasons)

    def test_skip_dirs_apply_by_default_and_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "node_modules").mkdir()
            (root / "src" / "app.py").write_text("needle", encoding="utf-8")
            (root / "node_modules" / "pkg.py").write_text("needle", encoding="utf-8")

            result = WorkspaceTraversalService(root).glob_files(pattern="**/*.py")

            self.assertEqual([Path(path).name for path in result.matches], ["app.py"])
            self.assertGreaterEqual(result.telemetry.dirs_skipped, 1)
            self.assertIn("skip_dir", result.telemetry.skipped_reasons)

    def test_grep_streams_without_path_read_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "note.txt").write_text("alpha\nneedle\n", encoding="utf-8")
            service = WorkspaceTraversalService(root)

            with patch.object(Path, "read_text", side_effect=AssertionError("read_text used")):
                result = service.grep_files(query="needle")

            self.assertEqual(len(result.matches), 1)
            self.assertEqual(result.matches[0]["line_number"], 2)

    def test_grep_cuts_by_max_file_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "big.txt").write_text("a" * 100 + "needle", encoding="utf-8")
            service = WorkspaceTraversalService(
                root, policy=TraversalPolicy(max_file_bytes=20, max_total_bytes=1_000)
            )

            result = service.grep_files(query="needle")

            self.assertEqual(result.matches, ())
            self.assertLessEqual(result.telemetry.bytes_scanned, 20)
            self.assertTrue(result.telemetry.truncated)
            self.assertIn("max_file_bytes", result.telemetry.skipped_reasons)

    def test_grep_cuts_by_max_total_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for idx in range(5):
                (root / f"file_{idx}.txt").write_text("a" * 20, encoding="utf-8")
            service = WorkspaceTraversalService(
                root, policy=TraversalPolicy(max_total_bytes=25, max_file_bytes=1_000)
            )

            result = service.grep_files(query="missing")

            self.assertLessEqual(result.telemetry.bytes_scanned, 25)
            self.assertTrue(result.telemetry.truncated)
            self.assertIn("max_total_bytes", result.telemetry.skipped_reasons)

    def test_grep_cuts_by_max_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "note.txt").write_text("needle\nneedle\nneedle\n", encoding="utf-8")
            service = WorkspaceTraversalService(root, policy=TraversalPolicy(max_matches=2))

            result = service.grep_files(query="needle")

            self.assertEqual(len(result.matches), 2)
            self.assertTrue(result.telemetry.truncated)
            self.assertIn("max_matches", result.telemetry.skipped_reasons)

    def test_grep_detects_binary_without_reading_entire_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "binary.bin").write_bytes(b"abc\x00needle")

            result = WorkspaceTraversalService(root).grep_files(query="needle")

            self.assertEqual(result.matches, ())
            self.assertEqual(result.telemetry.skipped_reasons.get("binary"), 1)

    def test_grep_handles_invalid_utf8_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "bad.txt").write_bytes(b"\xffneedle\n")

            result = WorkspaceTraversalService(root).grep_files(query="needle")

            self.assertEqual(len(result.matches), 1)
            self.assertIn("needle", result.matches[0]["line"])

    def test_symlink_file_outside_root_is_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root = base / "workspace"
            root.mkdir()
            outside = base / "outside.txt"
            outside.write_text("needle", encoding="utf-8")
            try:
                os.symlink(outside, root / "link.txt")
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            result = WorkspaceTraversalService(root).grep_files(query="needle")

            self.assertEqual(result.matches, ())
            self.assertIn("outside_root", result.telemetry.skipped_reasons)

    def test_symlink_directory_cycle_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "a").mkdir()
            (root / "a" / "note.txt").write_text("needle", encoding="utf-8")
            try:
                os.symlink(root, root / "a" / "loop")
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            result = WorkspaceTraversalService(root).grep_files(query="needle")

            self.assertEqual(len(result.matches), 1)
            self.assertIn("symlink_dir", result.telemetry.skipped_reasons)

    def test_root_outside_workspace_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root = base / "workspace"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "note.txt").write_text("needle", encoding="utf-8")

            result = WorkspaceTraversalService(root).glob_files(root=outside, pattern="**/*.txt")

            self.assertEqual(result.matches, ())
            self.assertIn("outside_root", result.telemetry.skipped_reasons)

    def test_deadline_uses_injected_clock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for idx in range(5):
                (root / f"file_{idx}.txt").write_text("needle", encoding="utf-8")
            service = WorkspaceTraversalService(
                root,
                policy=TraversalPolicy(deadline_ms=1),
                clock=_StepClock(step=0.01),
            )

            result = service.glob_files(pattern="**/*.txt")

            self.assertTrue(result.telemetry.deadline_exceeded)
            self.assertTrue(result.telemetry.truncated)

    def test_traversal_skip_dirs_are_not_secret_scanning_policy(self) -> None:
        self.assertIn("node_modules", DEFAULT_SKIP_DIRS)
        self.assertNotIn("scripts", DEFAULT_SKIP_DIRS)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / "_ignored_by_some_tools.py").write_text("needle", encoding="utf-8")

            result = WorkspaceTraversalService(root).grep_files(query="needle", pattern="**/*.py")

            self.assertEqual(len(result.matches), 1)
            self.assertIn("scripts", result.matches[0]["path"])


if __name__ == "__main__":
    unittest.main()
