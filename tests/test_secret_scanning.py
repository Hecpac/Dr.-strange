from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2 import secret_scanning
from claw_v2.secret_scanning import (
    EXIT_CLEAN,
    EXIT_ERROR,
    EXIT_FINDINGS,
    SOURCE_IGNORED,
    SOURCE_TRACKED,
    SOURCE_UNTRACKED_NONIGNORED,
    SecretScanConfig,
    discover_git_files,
    render_text,
    scan_repository,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
OPENAI_NAME = "OPENAI" + "_API_KEY"
FAL_NAME = "FAL" + "_KEY"
AUTH_BEARER = "Authorization" + ": Bearer "


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _init_repo(repo: Path) -> None:
    _git(repo, "init")


def _write_allowlist(repo: Path, entries: list[dict]) -> Path:
    path = repo / ".secret-scan-allowlist.json"
    path.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")
    return path


class SecretScanningTests(unittest.TestCase):
    def test_detects_tracked_untracked_and_ignored_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _init_repo(repo)
            (repo / ".gitignore").write_text("scripts/_*.py\n", encoding="utf-8")
            tracked_secret = "sk-live-local-openai-1234567890"
            fal_secret = "fal-local-secret-1234567890"
            bearer_secret = "abcdefghijklmnopqrstuvwxyz123456"
            (repo / "tracked.py").write_text(
                f'{OPENAI_NAME} = "{tracked_secret}"\n', encoding="utf-8"
            )
            _git(repo, "add", "tracked.py")
            (repo / "client.py").write_text(
                f"{AUTH_BEARER}{bearer_secret}\n", encoding="utf-8"
            )
            scripts = repo / "scripts"
            scripts.mkdir()
            (scripts / "_seedance_fal.py").write_text(
                f'os.environ["{FAL_NAME}"] = "{fal_secret}"\n', encoding="utf-8"
            )

            result = scan_repository(repo)

            triples = {(f.rule_id, f.source_set, f.path) for f in result.findings}
            self.assertIn(("openai_api_key_literal", SOURCE_TRACKED, "tracked.py"), triples)
            self.assertIn(("authorization_bearer", SOURCE_UNTRACKED_NONIGNORED, "client.py"), triples)
            self.assertIn(
                ("fal_key_literal", SOURCE_IGNORED, "scripts/_seedance_fal.py"),
                triples,
            )

    def test_git_discovery_keeps_three_source_sets_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _init_repo(repo)
            (repo / ".gitignore").write_text("scripts/_*.py\n", encoding="utf-8")
            (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
            (repo / "scripts").mkdir()
            (repo / "scripts" / "_ignored.py").write_text("ignored\n", encoding="utf-8")

            discovered = discover_git_files(repo)

            self.assertIn(Path("tracked.txt"), discovered[SOURCE_TRACKED])
            self.assertIn(Path("untracked.txt"), discovered[SOURCE_UNTRACKED_NONIGNORED])
            self.assertIn(Path("scripts/_ignored.py"), discovered[SOURCE_IGNORED])

    def test_discovery_uses_three_git_commands(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(list(command))
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("claw_v2.secret_scanning.subprocess.run", side_effect=fake_run):
                discover_git_files(Path(tmpdir))

        self.assertEqual(
            calls,
            [
                ["git", "ls-files", "-z"],
                ["git", "ls-files", "-o", "--exclude-standard", "-z"],
                ["git", "ls-files", "-o", "-i", "--exclude-standard", "-z"],
            ],
        )

    def test_reference_without_literal_is_not_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _init_repo(repo)
            (repo / "ref.py").write_text(
                f'key = os.environ["{FAL_NAME}"]\n', encoding="utf-8"
            )

            result = scan_repository(repo)

            self.assertEqual(result.findings, ())

    def test_output_redacts_complete_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _init_repo(repo)
            secret_value = "sk-live-local-redacted-1234567890"
            (repo / "tracked.py").write_text(
                f'{OPENAI_NAME} = "{secret_value}"\n', encoding="utf-8"
            )
            _git(repo, "add", "tracked.py")

            output = render_text(scan_repository(repo))

            self.assertEqual(output.find(secret_value), -1)
            self.assertIn("<REDACTED>", output)
            self.assertIn("sha256:", output)

    def test_no_workspace_traversal_skip_policy_import(self) -> None:
        source = Path(secret_scanning.__file__).read_text(encoding="utf-8")

        self.assertNotIn("DEFAULT_SKIP_DIRS", source)
        self.assertNotIn("workspace_traversal", source)

    def test_symlink_outside_root_is_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            repo = base / "repo"
            repo.mkdir()
            _init_repo(repo)
            outside = base / "outside.py"
            outside.write_text(
                f'{OPENAI_NAME} = "sk-live-outside-1234567890"\n',
                encoding="utf-8",
            )
            try:
                os.symlink(outside, repo / "leak.py")
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            result = scan_repository(repo)

            self.assertEqual(result.findings, ())
            self.assertIn("outside_root", {skipped.reason for skipped in result.skipped})

    def test_binary_file_is_reported_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _init_repo(repo)
            (repo / "blob.bin").write_bytes(
                f'\x00{OPENAI_NAME} = "sk-live-binary-1234567890"\n'.encode()
            )

            result = scan_repository(repo)

            self.assertEqual(result.findings, ())
            self.assertIn("binary", {skipped.reason for skipped in result.skipped})

    def test_too_large_file_is_reported_without_reading(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _init_repo(repo)
            (repo / "large.log").write_text("x" * 20, encoding="utf-8")

            result = scan_repository(repo, config=SecretScanConfig(max_file_bytes=5))

            self.assertEqual(result.findings, ())
            self.assertIn("too_large", {skipped.reason for skipped in result.skipped})

    def test_cli_exit_code_one_when_findings_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _init_repo(repo)
            secret_value = "sk-live-cli-1234567890"
            (repo / "tracked.py").write_text(
                f'{OPENAI_NAME} = "{secret_value}"\n', encoding="utf-8"
            )
            _git(repo, "add", "tracked.py")
            stdout = io.StringIO()

            code = secret_scanning.main(["--repo-root", str(repo)], stdout=stdout)

            self.assertEqual(code, EXIT_FINDINGS)
            self.assertEqual(stdout.getvalue().find(secret_value), -1)

    def test_cli_exit_code_zero_when_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _init_repo(repo)
            (repo / "tracked.py").write_text("print('clean')\n", encoding="utf-8")
            _git(repo, "add", "tracked.py")

            code = secret_scanning.main(["--repo-root", str(repo)], stdout=io.StringIO())

            self.assertEqual(code, EXIT_CLEAN)

    def test_cli_exit_code_two_on_execution_error(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = secret_scanning.main(
            ["--repo-root", "/path/that/does/not/exist"],
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(code, EXIT_ERROR)

    def test_allowlist_suppresses_exact_path_rule_and_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _init_repo(repo)
            secret_value = "sk-live-local-allowlist-1234567890"
            (repo / "tracked.py").write_text(
                f'{OPENAI_NAME} = "{secret_value}"\n', encoding="utf-8"
            )
            _git(repo, "add", "tracked.py")
            finding = scan_repository(repo).findings[0]
            allowlist = _write_allowlist(
                repo,
                [
                    {
                        "path": finding.path,
                        "rule_id": finding.rule_id,
                        "fingerprint": finding.fingerprint,
                        "classification": "test_fixture_safe",
                        "reason": "synthetic fixture",
                    }
                ],
            )

            result = scan_repository(repo, allowlist_path=allowlist)

            self.assertEqual(result.findings, ())
            self.assertEqual(len(result.suppressed_findings), 1)
            self.assertEqual(result.exit_code(), EXIT_CLEAN)

    def test_allowlist_does_not_suppress_path_rule_or_fingerprint_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _init_repo(repo)
            secret_value = "sk-live-local-drift-1234567890"
            (repo / "tracked.py").write_text(
                f'{OPENAI_NAME} = "{secret_value}"\n', encoding="utf-8"
            )
            _git(repo, "add", "tracked.py")
            finding = scan_repository(repo).findings[0]
            cases = (
                {"path": "other.py", "rule_id": finding.rule_id, "fingerprint": finding.fingerprint},
                {"path": finding.path, "rule_id": "generic_secret_assignment", "fingerprint": finding.fingerprint},
                {"path": finding.path, "rule_id": finding.rule_id, "fingerprint": "sha256:" + "0" * 64},
            )
            for idx, case in enumerate(cases):
                with self.subTest(case=idx):
                    allowlist = _write_allowlist(
                        repo,
                        [
                            {
                                **case,
                                "classification": "test_fixture_safe",
                                "reason": "synthetic fixture",
                            }
                        ],
                    )

                    result = scan_repository(repo, allowlist_path=allowlist)

                    self.assertEqual(len(result.findings), 1)
                    self.assertEqual(result.suppressed_findings, ())

    def test_allowlist_rejects_true_positive_unknown_wildcard_and_fal_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _init_repo(repo)
            base = {
                "path": "tracked.py",
                "rule_id": "openai_api_key_literal",
                "fingerprint": "sha256:" + "1" * 64,
                "classification": "test_fixture_safe",
                "reason": "synthetic fixture",
            }
            invalid_entries = (
                {**base, "classification": "true_positive_secret"},
                {**base, "classification": "unknown_requires_owner_review"},
                {**base, "path": "*.py"},
                {**base, "rule_id": "fal_key_literal"},
            )
            for idx, entry in enumerate(invalid_entries):
                with self.subTest(entry=idx):
                    allowlist = _write_allowlist(repo, [entry])

                    result = scan_repository(repo, allowlist_path=allowlist)

                    self.assertEqual(result.exit_code(), EXIT_ERROR)


class SecretScanWiringTests(unittest.TestCase):
    def test_github_workflow_runs_local_scanner_without_artifacts_or_secrets(self) -> None:
        workflow = REPO_ROOT / ".github" / "workflows" / "secret-scan.yml"
        text = workflow.read_text(encoding="utf-8")

        self.assertIn("python scripts/scan_secrets.py", text)
        self.assertIn("actions/checkout@v4", text)
        self.assertIn("actions/setup-python@v5", text)
        self.assertIn("contents: read", text)
        self.assertNotIn("upload-artifact", text)
        self.assertNotIn("${{ secrets.", text)

    def test_security_docs_describe_local_gate_exit_codes_and_ci_limit(self) -> None:
        doc = (REPO_ROOT / "claw_v2" / "SECURITY.md").read_text(encoding="utf-8")

        self.assertIn(".venv/bin/python scripts/scan_secrets.py", doc)
        self.assertIn("Exit codes: `0` means clean, `1` means findings", doc)
        self.assertIn("scripts/_*.py", doc)
        self.assertIn("Release gate", doc)
        self.assertIn("FAL_KEY", doc)
        self.assertIn("rotate it manually", doc)
        self.assertIn("ignored local files", doc)
        self.assertIn("Rotation is not automated", doc)


if __name__ == "__main__":
    unittest.main()
