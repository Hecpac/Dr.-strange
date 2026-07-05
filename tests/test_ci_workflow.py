from __future__ import annotations

from pathlib import Path


def test_ci_workflow_keeps_required_fast_gate_commands() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "uv sync --locked" in workflow
    assert "uv run ruff check" in workflow
    assert "uv run ruff format --check" in workflow
    assert "uv run pytest" in workflow
    assert "tests/test_secret_scanning.py" in workflow
