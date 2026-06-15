from __future__ import annotations

import unittest

from claw_v2.trivial_patch import TrivialPatchClassifier


def _diff(path: str, *changed_lines: str) -> str:
    body = "\n".join(changed_lines)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n{body}\n"


class TrivialPatchClassifierTests(unittest.TestCase):
    def test_docs_comments_accepted(self) -> None:
        docs = TrivialPatchClassifier().classify(
            changed_files=["docs/runbook.md"],
            diff=_diff("docs/runbook.md", "-Old note", "+New internal note"),
        )
        comments = TrivialPatchClassifier().classify(
            changed_files=["claw_v2/worker.py"],
            diff=_diff("claw_v2/worker.py", "-# Old comment", "+# Clearer comment"),
        )

        self.assertTrue(docs.trivial, docs.to_dict())
        self.assertIn("docs", docs.categories)
        self.assertTrue(comments.trivial, comments.to_dict())
        self.assertIn("comments", comments.categories)

    def test_typing_only_accepted(self) -> None:
        decision = TrivialPatchClassifier().classify(
            changed_files=["claw_v2/parser.py"],
            diff=_diff(
                "claw_v2/parser.py",
                "-from typing import Iterable",
                "+from typing import Sequence",
                "-def parse(value):",
                "+def parse(value: str) -> str:",
            ),
        )

        self.assertTrue(decision.trivial, decision.to_dict())
        self.assertIn("typing", decision.categories)

    def test_annotation_with_executable_rhs_rejected(self) -> None:
        # #1: `name: <expr>` is NOT typing-trivial — a module/class-level
        # annotated-assignment RHS is evaluated at import time, so an executable
        # RHS smuggled as an annotation is an RCE channel.
        decision = TrivialPatchClassifier().classify(
            changed_files=["claw_v2/worker.py"],
            diff=_diff(
                "claw_v2/worker.py",
                "+_warm: __import__('os').system('id')",
            ),
        )
        self.assertFalse(decision.trivial, decision.to_dict())

    def test_typing_import_with_smuggled_statement_rejected(self) -> None:
        # #2: a typing import line followed by `;` and an arbitrary statement
        # must not be classified trivial.
        decision = TrivialPatchClassifier().classify(
            changed_files=["claw_v2/worker.py"],
            diff=_diff(
                "claw_v2/worker.py",
                "+from typing import Any; CRED = open('/etc/passwd').read()",
            ),
        )
        self.assertFalse(decision.trivial, decision.to_dict())

    def test_test_only_accepted(self) -> None:
        decision = TrivialPatchClassifier().classify(
            changed_files=["tests/test_parser.py"],
            diff=_diff("tests/test_parser.py", "+assert parse('x') == 2"),
        )

        self.assertTrue(decision.trivial, decision.to_dict())
        self.assertIn("tests", decision.categories)

    def test_dev_deps_patch_level_accepted(self) -> None:
        decision = TrivialPatchClassifier().classify(
            changed_files=["requirements-dev.txt"],
            diff=_diff(
                "requirements-dev.txt",
                "-pytest==8.3.4",
                "+pytest==8.3.5",
            ),
        )

        self.assertTrue(decision.trivial, decision.to_dict())
        self.assertIn("dev_deps", decision.categories)

    def test_sensitive_paths_rejected(self) -> None:
        for path in (
            "claw_v2/approval.py",
            "claw_v2/auth/session.py",
            "claw_v2/crypto.py",
            "claw_v2/runtime_policy.py",
            "claw_v2/sandbox.py",
            "claw_v2/config.py",
            "ops/com.pachano.claw.plist",
            "MEMORY.md",
            "claw_v2/pipeline.py",
        ):
            with self.subTest(path=path):
                decision = TrivialPatchClassifier().classify(
                    changed_files=[path],
                    diff=_diff(path, "+# comment only"),
                )
                self.assertFalse(decision.trivial, decision.to_dict())
                self.assertIn("sensitive_paths", decision.reasons)

    def test_production_deps_rejected(self) -> None:
        decision = TrivialPatchClassifier().classify(
            changed_files=["requirements.txt"],
            diff=_diff("requirements.txt", "-requests==2.32.4", "+requests==2.32.5"),
        )

        self.assertFalse(decision.trivial, decision.to_dict())
        self.assertIn("production_deps", decision.reasons)

    def test_ambiguous_unknown_patch_rejected(self) -> None:
        decision = TrivialPatchClassifier().classify(
            changed_files=["claw_v2/scoring.py"],
            diff=_diff("claw_v2/scoring.py", "-return score", "+return score + 2"),
        )

        self.assertFalse(decision.trivial, decision.to_dict())
        self.assertIn("unknown_or_non_trivial_patch", decision.reasons)

    def test_missing_metadata_rejected(self) -> None:
        missing_diff = TrivialPatchClassifier().classify(changed_files=["docs/runbook.md"])
        missing_files = TrivialPatchClassifier().classify(
            diff=_diff("docs/runbook.md", "+New note")
        )

        self.assertFalse(missing_diff.trivial)
        self.assertIn("missing_diff", missing_diff.reasons)
        self.assertFalse(missing_files.trivial)
        self.assertIn("missing_changed_files", missing_files.reasons)

    def test_fail_closed_default(self) -> None:
        decision = TrivialPatchClassifier().classify()

        self.assertFalse(decision.trivial)
        self.assertIn("missing_changed_files", decision.reasons)
        self.assertIn("missing_diff", decision.reasons)


if __name__ == "__main__":
    unittest.main()
