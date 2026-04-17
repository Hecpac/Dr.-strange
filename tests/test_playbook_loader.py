"""Tests for the PlaybookLoader system."""
from pathlib import Path
import tempfile

from claw_v2.playbook_loader import PlaybookLoader, _parse_playbook


def _write_playbook(directory: Path, name: str, triggers: list[str], body: str, priority: int = 0) -> Path:
    lines = ["---", f"name: {name}", "triggers:"]
    for t in triggers:
        lines.append(f"  - {t}")
    if priority:
        lines.append(f"priority: {priority}")
    lines.extend(["---", "", body])
    path = directory / f"{name.lower().replace(' ', '_')}.md"
    path.write_text("\n".join(lines))
    return path


class TestParsePlaybook:
    def test_valid_playbook(self, tmp_path: Path) -> None:
        path = _write_playbook(tmp_path, "Test Skill", ["keyword1", "keyword2"], "Do the thing.")
        pb = _parse_playbook(path)
        assert pb is not None
        assert pb.name == "Test Skill"
        assert pb.triggers == ["keyword1", "keyword2"]
        assert "Do the thing." in pb.content

    def test_no_frontmatter(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.md"
        path.write_text("Just some text without frontmatter.")
        assert _parse_playbook(path) is None

    def test_no_triggers(self, tmp_path: Path) -> None:
        path = tmp_path / "no_triggers.md"
        path.write_text("---\nname: Empty\n---\nBody text.")
        assert _parse_playbook(path) is None

    def test_priority(self, tmp_path: Path) -> None:
        path = _write_playbook(tmp_path, "High", ["x"], "body", priority=10)
        pb = _parse_playbook(path)
        assert pb is not None
        assert pb.priority == 10


class TestPlaybookLoader:
    def test_load_and_match(self, tmp_path: Path) -> None:
        _write_playbook(tmp_path, "NLM", ["notebooklm", "podcast"], "NLM workflow")
        _write_playbook(tmp_path, "Trading", ["backtest", "qts"], "Trading workflow")
        loader = PlaybookLoader()
        loader.load(tmp_path)
        assert len(loader.playbooks) == 2

        matched = loader.match("Crea un notebook en notebooklm")
        assert len(matched) == 1
        assert matched[0].name == "NLM"

    def test_no_match(self, tmp_path: Path) -> None:
        _write_playbook(tmp_path, "NLM", ["notebooklm"], "body")
        loader = PlaybookLoader()
        loader.load(tmp_path)
        assert loader.match("hello world") == []

    def test_multi_trigger_ranking(self, tmp_path: Path) -> None:
        _write_playbook(tmp_path, "A", ["chrome", "browser"], "A body")
        _write_playbook(tmp_path, "B", ["chrome", "cdp", "playwright"], "B body")
        loader = PlaybookLoader()
        loader.load(tmp_path)
        matched = loader.match("usa chrome cdp con playwright")
        assert matched[0].name == "B"

    def test_priority_tiebreak(self, tmp_path: Path) -> None:
        _write_playbook(tmp_path, "Low", ["keyword"], "low body", priority=1)
        _write_playbook(tmp_path, "High", ["keyword"], "high body", priority=10)
        loader = PlaybookLoader()
        loader.load(tmp_path)
        matched = loader.match("keyword test")
        assert matched[0].name == "High"

    def test_context_for_wraps_in_tags(self, tmp_path: Path) -> None:
        _write_playbook(tmp_path, "NLM", ["notebook"], "workflow here")
        loader = PlaybookLoader()
        loader.load(tmp_path)
        ctx = loader.context_for("crea un notebook")
        assert ctx.startswith("<playbook-context>")
        assert ctx.endswith("</playbook-context>")
        assert "workflow here" in ctx

    def test_context_for_empty_on_no_match(self, tmp_path: Path) -> None:
        _write_playbook(tmp_path, "NLM", ["notebook"], "body")
        loader = PlaybookLoader()
        loader.load(tmp_path)
        assert loader.context_for("random question") == ""

    def test_max_results(self, tmp_path: Path) -> None:
        for i in range(5):
            _write_playbook(tmp_path, f"PB{i}", ["common"], f"body {i}")
        loader = PlaybookLoader()
        loader.load(tmp_path)
        assert len(loader.match("common", max_results=2)) == 2

    def test_auto_loads_on_first_match(self, tmp_path: Path) -> None:
        _write_playbook(tmp_path, "Auto", ["autoload"], "auto body")
        loader = PlaybookLoader()
        loader.load(tmp_path)
        assert loader._loaded is True
        matched = loader.match("test autoload")
        assert len(matched) == 1
