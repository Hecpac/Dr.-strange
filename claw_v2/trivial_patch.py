from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from claw_v2.approval_sensitivity import classify_sensitive_change


_DOC_EXTENSIONS = frozenset({".md", ".rst", ".txt"})
_TEST_EXTENSIONS = frozenset({".py", ".js", ".jsx", ".ts", ".tsx"})
_DEV_DEP_FILES = frozenset(
    {
        "requirements-dev.txt",
        "dev-requirements.txt",
        "requirements_test.txt",
        "requirements-test.txt",
    }
)
_PROD_DEP_FILES = frozenset(
    {
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "pipfile",
        "gemfile",
        "cargo.toml",
        "go.mod",
        "pom.xml",
    }
)
_LOCKFILES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pipfile.lock",
        "gemfile.lock",
        "cargo.lock",
        "go.sum",
        "uv.lock",
    }
)
_MEMORY_FILES = frozenset(
    {
        "agents.md",
        "boot_protocol.md",
        "identity.md",
        "memory.md",
        "soul.md",
        "user.md",
    }
)
_PY_SOURCE_SUFFIXES = (".py", ".pyi")
_REQUIREMENT_CHANGE_RE = re.compile(
    r"^[+-]\s*([A-Za-z0-9_.-]+)==(\d+)\.(\d+)\.(\d+)(?:\b|$)"
)


@dataclass(frozen=True, slots=True)
class TrivialPatchDecision:
    trivial: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    categories: tuple[str, ...] = field(default_factory=tuple)
    sensitive_paths: tuple[str, ...] = field(default_factory=tuple)

    @property
    def allowed(self) -> bool:
        return self.trivial

    def to_dict(self) -> dict[str, Any]:
        return {
            "trivial": self.trivial,
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "categories": list(self.categories),
            "sensitive_paths": list(self.sensitive_paths),
        }


class TrivialPatchClassifier:
    """Fail-closed classifier for patches that are safe to consider trivial."""

    def classify(
        self,
        *,
        changed_files: Iterable[str] | None = None,
        diff: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> TrivialPatchDecision:
        metadata = dict(metadata or {})
        if changed_files is None:
            changed_files = _coerce_changed_files(metadata.get("changed_files") or metadata.get("paths"))
        if diff is None:
            diff = _coerce_diff(metadata.get("diff"))

        normalized_files = tuple(_normalize_path(path) for path in (changed_files or ()) if str(path).strip())
        diff_text = str(diff or "")
        reasons: list[str] = []
        categories: set[str] = set()

        if not normalized_files:
            reasons.append("missing_changed_files")
        if not diff_text.strip():
            reasons.append("missing_diff")

        sensitivity = classify_sensitive_change(
            diff=diff_text,
            paths=normalized_files,
            action="trivial_patch_classification",
            summary="classify patch for fail-closed triviality",
        )

        direct_denials = _direct_denied_paths(normalized_files)
        if direct_denials:
            reasons.append("sensitive_paths")

        sensitivity_denied = _sensitivity_requires_rejection(
            categories=sensitivity.categories,
            paths=normalized_files,
        )
        if sensitivity.sensitive and sensitivity_denied:
            reasons.append("sensitive_paths")

        if _touches_production_dependencies(normalized_files):
            reasons.append("production_deps")

        if normalized_files and diff_text.strip():
            paths_in_diff = _paths_from_diff(diff_text)
            if paths_in_diff and not set(normalized_files).issubset(paths_in_diff):
                reasons.append("changed_files_diff_mismatch")
            for path in normalized_files:
                category = _trivial_category(path, diff_text)
                if category is None:
                    reasons.append("unknown_or_non_trivial_patch")
                else:
                    categories.add(category)

        deduped_reasons = tuple(dict.fromkeys(reasons))
        return TrivialPatchDecision(
            trivial=not deduped_reasons,
            reasons=deduped_reasons,
            categories=tuple(sorted(categories)),
            sensitive_paths=tuple(sorted(set(sensitivity.sensitive_paths) | set(direct_denials))),
        )


def _coerce_changed_files(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    try:
        return tuple(str(item) for item in value)
    except TypeError:
        return None


def _coerce_diff(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _normalize_path(path: str) -> str:
    return str(path).replace("\\", "/").strip().lstrip("./")


def _direct_denied_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    denied: list[str] = []
    for path in paths:
        lowered = path.lower()
        name = PurePosixPath(lowered).name
        if (
            name in _MEMORY_FILES
            or lowered.startswith("memory/")
            or "/memory/" in lowered
            or lowered.startswith("data/")
            or _is_launchd_path(lowered)
            or _is_pipeline_or_merge_path(lowered)
            or _is_runtime_path(lowered)
        ):
            denied.append(path)
    return tuple(denied)


def _is_launchd_path(lowered_path: str) -> bool:
    name = PurePosixPath(lowered_path).name
    return lowered_path.startswith("ops/") and (name.endswith(".plist") or "launch" in name)


def _is_pipeline_or_merge_path(lowered_path: str) -> bool:
    name = PurePosixPath(lowered_path).name
    return (
        name == "pipeline.py"
        or "/pipeline/" in lowered_path
        or "pipeline_merge" in lowered_path
        or "automerge" in lowered_path
        or "auto_merge" in lowered_path
        or ("merge" in lowered_path and lowered_path.startswith((".github/", "scripts/")))
    )


def _is_runtime_path(lowered_path: str) -> bool:
    name = PurePosixPath(lowered_path).name
    return (
        "runtime" in lowered_path
        or name in {"main.py", "daemon.py", "lifecycle.py", "runtime_policy.py"}
    )


def _sensitivity_requires_rejection(*, categories: tuple[str, ...], paths: tuple[str, ...]) -> bool:
    category_set = set(categories)
    if not category_set:
        return False
    dev_dep_only = all(_is_dev_dep_file(path) for path in paths)
    if dev_dep_only and category_set.issubset({"deps", "package_manifest"}):
        return False
    return True


def _touches_production_dependencies(paths: tuple[str, ...]) -> bool:
    for path in paths:
        name = PurePosixPath(path.lower()).name
        if _is_dev_dep_file(path):
            continue
        if name in _PROD_DEP_FILES or name in _LOCKFILES:
            return True
    return False


def _trivial_category(path: str, diff: str) -> str | None:
    lowered = path.lower()
    suffix = PurePosixPath(lowered).suffix
    if _is_dev_dep_file(lowered):
        return "dev_deps" if _is_dev_dependency_patch_level(diff, path) else None
    if _is_doc_path(lowered):
        return "docs"
    if _is_test_path(lowered):
        return "tests"
    if suffix == ".pyi":
        return "typing"
    if lowered.endswith(_PY_SOURCE_SUFFIXES):
        changed_lines = _changed_lines_for_path(diff, path)
        if not changed_lines:
            return None
        if all(_is_comment_line(line) for line in changed_lines):
            return "comments"
        if _is_python_typing_only_change(changed_lines):
            return "typing"
    return None


def _is_doc_path(lowered_path: str) -> bool:
    name = PurePosixPath(lowered_path).name
    suffix = PurePosixPath(lowered_path).suffix
    if name in _MEMORY_FILES:
        return False
    return lowered_path.startswith("docs/") or lowered_path.startswith("internal_docs/") or suffix in _DOC_EXTENSIONS


def _is_test_path(lowered_path: str) -> bool:
    suffix = PurePosixPath(lowered_path).suffix
    return (lowered_path.startswith("tests/") or "/tests/" in lowered_path) and suffix in _TEST_EXTENSIONS


def _is_dev_dep_file(path: str) -> bool:
    lowered = path.lower()
    name = PurePosixPath(lowered).name
    return name in _DEV_DEP_FILES or lowered.startswith(("requirements/dev", "requirements/test"))


def _is_dev_dependency_patch_level(diff: str, path: str) -> bool:
    removed: dict[str, tuple[str, str, str]] = {}
    added: dict[str, tuple[str, str, str]] = {}
    for raw in _changed_lines_for_path(diff, path):
        if _is_comment_line(raw):
            continue
        match = _REQUIREMENT_CHANGE_RE.match(raw)
        if match is None:
            return False
        package, major, minor, patch = match.groups()
        target = added if raw.startswith("+") else removed
        target[package.lower()] = (major, minor, patch)
    if not added or set(added) != set(removed):
        return False
    for package, added_version in added.items():
        removed_version = removed[package]
        if added_version[:2] != removed_version[:2]:
            return False
        if added_version[2] == removed_version[2]:
            return False
    return True


def _changed_lines_for_path(diff: str, path: str) -> list[str]:
    target = _normalize_path(path)
    current_file = ""
    lines: list[str] = []
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            current_file = _strip_diff_path(raw.split(maxsplit=1)[1])
            continue
        if current_file != target:
            continue
        if raw.startswith(("+++", "---")):
            continue
        if raw.startswith(("+", "-")):
            lines.append(raw)
    return lines


def _paths_from_diff(diff: str) -> set[str]:
    paths: set[str] = set()
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            parts = raw.split()
            for item in parts[2:4]:
                path = _strip_diff_path(item)
                if path:
                    paths.add(path)
        elif raw.startswith(("+++ ", "--- ")):
            path = _strip_diff_path(raw.split(maxsplit=1)[1])
            if path:
                paths.add(path)
    return paths


def _strip_diff_path(raw: str) -> str:
    value = raw.strip()
    if value in {"/dev/null", "dev/null"}:
        return ""
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return _normalize_path(value)


def _is_comment_line(raw_line: str) -> bool:
    if not raw_line.startswith(("+", "-")):
        return False
    line = raw_line[1:].strip()
    return not line or line.startswith(("#", "//", "/*", "*", "*/"))


def _is_typing_line(raw_line: str) -> bool:
    if not raw_line.startswith(("+", "-")):
        return False
    line = raw_line[1:].strip()
    if line.startswith(("from typing import ", "import typing", "if TYPE_CHECKING:")):
        return True
    if line.startswith(("def ", "async def ")):
        return "->" in line or bool(re.search(r"\([^)]*:\s*[^)]+\)", line))
    if line.startswith("type ") and "=" in line:
        return True
    return bool(re.match(r"[A-Za-z_][\w.]*\s*:\s*[^=]+$", line))


def _is_python_typing_only_change(changed_lines: list[str]) -> bool:
    if all(_is_comment_line(line) or _is_typing_line(line) for line in changed_lines):
        return True
    non_typing = [
        line
        for line in changed_lines
        if not _is_comment_line(line) and not _is_typing_line(line)
    ]
    if not non_typing or not all(_is_untyped_def_line(line) for line in non_typing):
        return False
    typed_defs = {
        _def_name(line)
        for line in changed_lines
        if _is_typing_line(line) and _def_name(line)
    }
    return all(_def_name(line) in typed_defs for line in non_typing)


def _is_untyped_def_line(raw_line: str) -> bool:
    if not raw_line.startswith(("+", "-")):
        return False
    line = raw_line[1:].strip()
    return line.startswith(("def ", "async def ")) and "->" not in line and not re.search(r"\([^)]*:\s*[^)]+\)", line)


def _def_name(raw_line: str) -> str | None:
    if not raw_line.startswith(("+", "-")):
        return None
    line = raw_line[1:].strip()
    match = re.match(r"(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", line)
    if match is None:
        return None
    return match.group(1)
