"""F0.1 — scratch atomic write.

Pins that coordinator phase artifacts are written atomically (temp file +
fsync + os.replace + parent-dir fsync), so a crash between temp-write and
rename never leaves a half-written ``*.json`` for ``_load_scratch_results``
to pick up. Mirrors ``approval.py:_atomic_write_json`` but adds the
directory fsync that helper omits.
"""

from __future__ import annotations

import os

import pytest

import claw_v2.coordinator as cmod
from claw_v2.coordinator import CoordinatorService, WorkerResult


def _coord(tmp_path):
    return CoordinatorService(router=object(), observe=object(), scratch_root=tmp_path)


def test_load_scratch_results_never_sees_truncated_json(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    results = [WorkerResult(task_name="w1", content="hello world", duration_seconds=1.0)]

    def boom(_src, _dst):
        raise OSError("simulated crash between temp-write and rename")

    monkeypatch.setattr(cmod.os, "replace", boom)

    with pytest.raises(OSError):
        coord._write_scratch(tmp_path / "task1", "research", results)

    # Final target is absent (never half-written), so the loader returns
    # nothing rather than a partial/corrupt result.
    loaded = coord._load_scratch_results("task1", "research")
    assert loaded == []

    phase_dir = tmp_path / "task1" / "research"
    # No leftover *.json (the dot-prefixed tmp must not match the glob), and
    # the tmp was cleaned up best-effort.
    assert list(phase_dir.glob("*.json")) == []
    assert list(phase_dir.glob(".*")) == []


def test_load_scratch_results_reads_full_result_after_atomic_write(tmp_path):
    coord = _coord(tmp_path)
    results = [WorkerResult(task_name="w1", content="hello world", duration_seconds=2.5)]

    coord._write_scratch(tmp_path / "task1", "research", results)

    loaded = coord._load_scratch_results("task1", "research")
    assert len(loaded) == 1
    assert loaded[0].task_name == "w1"
    assert loaded[0].content == "hello world"
    assert loaded[0].duration_seconds == 2.5


def test_atomic_write_text_fsyncs_parent_directory(tmp_path, monkeypatch):
    target = tmp_path / "phase" / "w1.json"
    target.parent.mkdir(parents=True)

    opened_dirs: list[str] = []
    real_open = cmod.os.open

    def spy_open(path, flags, *args, **kwargs):
        if os.path.isdir(path):
            opened_dirs.append(str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(cmod.os, "open", spy_open)

    cmod._atomic_write_text(target, '{"task_name": "w1"}')

    # Durability requirement: the parent directory is fsynced (the bit the
    # approval helper omits). os.open on the parent dir is the signal.
    assert str(target.parent) in opened_dirs
    assert target.read_text(encoding="utf-8") == '{"task_name": "w1"}'


def test_write_scratch_text_is_atomic(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    scratch = tmp_path / "task1"
    scratch.mkdir(parents=True)

    def boom(_src, _dst):
        raise OSError("simulated crash")

    monkeypatch.setattr(cmod.os, "replace", boom)
    with pytest.raises(OSError):
        coord._write_scratch_text(scratch, "synthesis.md", "partial content")

    assert not (scratch / "synthesis.md").exists()
    assert list(scratch.glob(".*")) == []
