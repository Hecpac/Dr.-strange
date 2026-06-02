from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping


_NUMBER_RE = re.compile(r"(?<![\w.])-?(?:\d+\.\d+|\d+)(?![\w.])")
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_ALL_CAPS_ASSIGNMENT_RE = re.compile(r"^\s*[A-Z][A-Z0-9_]*\s*(?::[^=]+)?=")
_ENUM_CONTEXT_RE = re.compile(r"\b(?:Enum|IntEnum|StrEnum|Literal)\b")
_JUSTIFICATION_KEYS = (
    "no_fudge_justification",
    "numeric_justification",
    "fudge_factor_justification",
)
_JUSTIFICATION_MARKERS = (
    "no-fudge:",
    "numeric-justification:",
    "fudge-factor:",
)
_TRIVIAL_CONSTANTS = {"0", "1", "-1", "0.0", "1.0", "-1.0"}


@dataclass(frozen=True, slots=True)
class NoFudgeFinding:
    file_path: str
    line: str
    constants: tuple[str, ...]
    line_number: int | None = None
    reason: str = "unjustified_numeric_constant"

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "line_number": self.line_number,
            "line": self.line,
            "constants": list(self.constants),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class NoFudgeReport:
    status: str
    findings: tuple[NoFudgeFinding, ...] = field(default_factory=tuple)
    requires_human_approval: bool = False

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "requires_human_approval": self.requires_human_approval,
            "findings": [finding.to_dict() for finding in self.findings],
        }


def validate_no_fudge_factors(
    diff: str,
    *,
    evidence: Mapping[str, Any] | None = None,
) -> NoFudgeReport:
    """Reject new suspicious numeric constants unless the diff justifies them."""

    evidence = dict(evidence or {})
    if _has_numeric_justification(evidence):
        return NoFudgeReport(status="passed")

    findings: list[NoFudgeFinding] = []
    current_file = ""
    new_line_number: int | None = None
    for raw_line in str(diff or "").splitlines():
        file_match = _DIFF_FILE_RE.match(raw_line)
        if file_match:
            current_file = file_match.group(1)
            new_line_number = None
            continue
        hunk_match = _HUNK_RE.match(raw_line)
        if hunk_match:
            new_line_number = int(hunk_match.group(1))
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            line_number = new_line_number
            if new_line_number is not None:
                new_line_number += 1
            line = raw_line[1:]
            constants = tuple(_suspicious_constants(current_file, line))
            if constants:
                findings.append(
                    NoFudgeFinding(
                        file_path=current_file or "<unknown>",
                        line_number=line_number,
                        line=line.strip()[:240],
                        constants=constants,
                    )
                )
            continue
        if raw_line.startswith(" ") and new_line_number is not None:
            new_line_number += 1

    if findings:
        return NoFudgeReport(
            status="blocked",
            findings=tuple(findings),
            requires_human_approval=True,
        )
    return NoFudgeReport(status="passed")


def _suspicious_constants(file_path: str, line: str) -> list[str]:
    if _is_allowed_context(file_path, line):
        return []
    constants: list[str] = []
    for value in _NUMBER_RE.findall(line):
        if _is_allowed_numeric_constant(value, line):
            continue
        constants.append(value)
    return constants


def _has_numeric_justification(evidence: Mapping[str, Any]) -> bool:
    for key in _JUSTIFICATION_KEYS:
        value = evidence.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list) and any(str(item).strip() for item in value):
            return True
    return False


def _line_has_inline_justification(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in _JUSTIFICATION_MARKERS)


def _is_allowed_context(file_path: str, line: str) -> bool:
    normalized_path = file_path.lower()
    stripped = line.strip()
    if not stripped:
        return True
    if _is_test_fixture_path(normalized_path):
        return True
    if _line_has_inline_justification(line):
        return True
    if _ALL_CAPS_ASSIGNMENT_RE.match(stripped):
        return True
    if _ENUM_CONTEXT_RE.search(stripped):
        return True
    if stripped.startswith(("#", "//", "/*", "*")):
        return True
    return False


def _is_test_fixture_path(normalized_path: str) -> bool:
    return (
        normalized_path.startswith("tests/")
        or "/tests/" in normalized_path
        or normalized_path.startswith("test/")
        or "/fixtures/" in normalized_path
        or "fixture" in normalized_path
    )


def _is_allowed_numeric_constant(value: str, line: str) -> bool:
    normalized = value.rstrip("0").rstrip(".") if "." in value else value
    if value in _TRIVIAL_CONSTANTS or normalized in _TRIVIAL_CONSTANTS:
        return True
    return _is_trivial_length_context(value, line)


def _is_trivial_length_context(value: str, line: str) -> bool:
    try:
        number = int(value)
    except ValueError:
        return False
    if number < 2 or number > 10:
        return False
    return any(marker in line for marker in ("len(", "range(", ".split(", "[:", "chunk_size"))
