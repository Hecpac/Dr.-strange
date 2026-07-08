from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

# Parity lock for the restart backup dir resolution (debt from the PR #238
# review, minimax P3 #1/#2 — investigated 2026-07-08 and REFUTED as bugs):
#
# The Python resolver (claw_v2/main.py:_restart_backup_dir) and the shell
# consumers (scripts/restart.sh:15, ops/claw-launcher.sh:69, both
# `${CLAW_RESTART_DB_BACKUP_DIR:-data/backups/restart}` after cd-ing to the
# repo root) AGREE on every audited input class:
#
#   - unset        -> default (repo_root/data/backups/restart)
#   - empty string -> default (shell `:-` and Python `if env:` both treat
#                     explicit-empty as unset — neither layer distinguishes)
#   - absolute     -> used verbatim
#   - relative     -> anchored at the repo root (both processes cd there)
#   - literal `~`  -> NOT expanded by either side (the shell does not
#                     tilde-expand a value stored in a variable; Path() keeps
#                     it literal) — contrary to the review's claim
#
# The docstring's "computed EXACTLY as the shell does" is therefore TRUE
# today. These tests lock that parity: a future edit that "fixes" one side
# alone (e.g. adding expanduser() to Python, or switching the shell to `-`
# instead of `:-`) breaks the contract loudly here instead of silently
# diverging in production.

REPO_ROOT = Path(__file__).resolve().parents[1]
SHELL_DEFAULT_EXPR = "${CLAW_RESTART_DB_BACKUP_DIR:-data/backups/restart}"


def _python_resolution(value: str | None) -> Path:
    from claw_v2 import main as main_module

    env: dict[str, str] = {}
    if value is not None:
        env["CLAW_RESTART_DB_BACKUP_DIR"] = value
    with patch.dict(os.environ, env, clear=False):
        if value is None:
            os.environ.pop("CLAW_RESTART_DB_BACKUP_DIR", None)
        result = main_module._restart_backup_dir(Path("data") / "claw.db")
    # Anchor relative results the way the daemon does: its cwd is the repo
    # root (claw-launcher.sh cds there before exec).
    return result if result.is_absolute() else REPO_ROOT / result


def _shell_resolution(value: str | None) -> Path:
    env = {k: v for k, v in os.environ.items() if k != "CLAW_RESTART_DB_BACKUP_DIR"}
    if value is not None:
        env["CLAW_RESTART_DB_BACKUP_DIR"] = value
    out = subprocess.run(
        ["bash", "-c", f'printf %s "{SHELL_DEFAULT_EXPR}"'],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,  # restart.sh:3 / claw-launcher.sh:34 cd to the repo root
        check=True,
    ).stdout
    p = Path(out)
    return p if p.is_absolute() else REPO_ROOT / p


PARITY_CASES: list[tuple[str, str | None]] = [
    ("unset", None),
    ("explicit empty string", ""),
    ("absolute path", "/tmp/claw-parity-abs"),
    ("relative path", "custom/backups"),
    ("literal tilde", "~/claw-parity-tilde"),
    ("path with spaces", "/tmp/claw parity spaces"),
]


class RestartBackupDirParityTests(unittest.TestCase):
    def test_python_and_shell_resolve_identically(self) -> None:
        for label, value in PARITY_CASES:
            with self.subTest(case=label, value=value):
                self.assertEqual(
                    _python_resolution(value),
                    _shell_resolution(value),
                    f"python and shell diverged for {label!r} — the "
                    "'computed EXACTLY as the shell does' contract broke",
                )

    def test_default_is_the_repo_root_production_dir(self) -> None:
        self.assertEqual(
            _python_resolution(None),
            REPO_ROOT / "data" / "backups" / "restart",
        )

    def test_tilde_is_literal_on_both_sides(self) -> None:
        # Refutes the PR #238 review claim: neither side expands a stored ~.
        py = _python_resolution("~/claw-parity-tilde")
        sh = _shell_resolution("~/claw-parity-tilde")
        self.assertEqual(py, sh)
        self.assertIn("~", str(py), "a side started expanding ~ — parity contract edit required")


if __name__ == "__main__":
    unittest.main()
