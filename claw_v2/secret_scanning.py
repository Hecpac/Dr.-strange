from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TextIO


SOURCE_TRACKED = "tracked"
SOURCE_UNTRACKED_NONIGNORED = "untracked_nonignored"
SOURCE_IGNORED = "ignored"
SOURCE_SETS: tuple[str, ...] = (
    SOURCE_TRACKED,
    SOURCE_UNTRACKED_NONIGNORED,
    SOURCE_IGNORED,
)

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

_PLACEHOLDER_VALUES = {
    "",
    "changeme",
    "change-me",
    "change_me",
    "example",
    "dummy",
    "test",
}
_REDACTION = "<REDACTED>"
_DEFAULT_MAX_FILE_BYTES = 1_000_000
_BINARY_SAMPLE_BYTES = 4096
_DEFAULT_ALLOWLIST = ".secret-scan-allowlist.json"
_GIT_COMMAND_TIMEOUT_SECONDS = 30
_ALLOWED_SUPPRESSION_CLASSIFICATIONS = frozenset(
    {
        "test_fixture_safe",
        "documentation_example_safe",
        "placeholder_safe",
        "false_positive_rule_noise",
    }
)
_UNSUPPRESSABLE_RULE_IDS = frozenset({"fal_key_literal"})


@dataclass(frozen=True, slots=True)
class SecretRule:
    rule_id: str
    pattern: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class SecretFinding:
    path: str
    line_number: int
    source_set: str
    rule_id: str
    redacted_preview: str
    fingerprint: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "line_number": self.line_number,
            "source_set": self.source_set,
            "rule_id": self.rule_id,
            "redacted_preview": self.redacted_preview,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class SuppressedFinding:
    path: str
    line_number: int
    source_set: str
    rule_id: str
    fingerprint: str
    classification: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "line_number": self.line_number,
            "source_set": self.source_set,
            "rule_id": self.rule_id,
            "fingerprint": self.fingerprint,
            "classification": self.classification,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SkippedFile:
    path: str
    source_set: str
    reason: str

    def to_dict(self) -> dict:
        return {"path": self.path, "source_set": self.source_set, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class SecretScanResult:
    findings: tuple[SecretFinding, ...]
    suppressed_findings: tuple[SuppressedFinding, ...] = ()
    skipped: tuple[SkippedFile, ...] = ()
    errors: tuple[str, ...] = ()

    def exit_code(self) -> int:
        if self.errors:
            return EXIT_ERROR
        if self.findings:
            return EXIT_FINDINGS
        return EXIT_CLEAN

    def to_dict(self) -> dict:
        return {
            "findings": [finding.to_dict() for finding in self.findings],
            "suppressed_findings": [
                suppressed.to_dict() for suppressed in self.suppressed_findings
            ],
            "skipped": [skipped.to_dict() for skipped in self.skipped],
            "errors": list(self.errors),
        }


@dataclass(frozen=True, slots=True)
class SecretScanConfig:
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES
    binary_sample_bytes: int = _BINARY_SAMPLE_BYTES


@dataclass(frozen=True, slots=True)
class AllowlistEntry:
    path: str
    rule_id: str
    fingerprint: str
    classification: str
    reason: str

    def key(self) -> tuple[str, str, str]:
        return (self.path, self.rule_id, self.fingerprint)


def _quoted_assignment_pattern(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?:^|[^\w])(?:export\s+)?{re.escape(name)}\s*=\s*"
        r"(?P<quote>['\"])(?P<value>[^'\"]+)(?P=quote)"
    )


def _env_assignment_pattern(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"os\.environ\[\s*['\"]{re.escape(name)}['\"]\s*\]\s*=\s*"
        r"(?P<quote>['\"])(?P<value>[^'\"]+)(?P=quote)"
    )


SECRET_RULES: tuple[SecretRule, ...] = (
    SecretRule("fal_key_literal", _env_assignment_pattern("FAL_KEY")),
    SecretRule("fal_key_literal", _quoted_assignment_pattern("FAL_KEY")),
    SecretRule("openai_api_key_literal", _env_assignment_pattern("OPENAI_API_KEY")),
    SecretRule("openai_api_key_literal", _quoted_assignment_pattern("OPENAI_API_KEY")),
    SecretRule("anthropic_api_key_literal", _env_assignment_pattern("ANTHROPIC_API_KEY")),
    SecretRule("anthropic_api_key_literal", _quoted_assignment_pattern("ANTHROPIC_API_KEY")),
    SecretRule(
        "generic_secret_assignment",
        re.compile(
            r"(?:^|[^\w])(?:export\s+)?[A-Z0-9_]*(?:KEY|TOKEN|SECRET)\s*=\s*"
            r"(?P<quote>['\"])(?P<value>[^'\"]{12,})(?P=quote)"
        ),
    ),
    SecretRule(
        "generic_secret_assignment",
        re.compile(
            r"os\.environ\[\s*['\"][A-Z0-9_]*(?:KEY|TOKEN|SECRET)['\"]\s*\]\s*=\s*"
            r"(?P<quote>['\"])(?P<value>[^'\"]{12,})(?P=quote)"
        ),
    ),
    SecretRule(
        "authorization_bearer",
        re.compile(r"Authorization\s*:\s*Bearer\s+(?P<value>[A-Za-z0-9._~+/=-]{12,})"),
    ),
)


def discover_git_files(repo_root: str | Path) -> dict[str, tuple[Path, ...]]:
    root = Path(repo_root).resolve(strict=False)
    commands: tuple[tuple[str, tuple[str, ...]], ...] = (
        (SOURCE_TRACKED, ("git", "ls-files", "-z")),
        (SOURCE_UNTRACKED_NONIGNORED, ("git", "ls-files", "-o", "--exclude-standard", "-z")),
        (SOURCE_IGNORED, ("git", "ls-files", "-o", "-i", "--exclude-standard", "-z")),
    )
    discovered: dict[str, tuple[Path, ...]] = {}
    for source_set, command in commands:
        completed = subprocess.run(
            list(command),
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_GIT_COMMAND_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"{source_set} discovery failed: {detail[:200]}")
        discovered[source_set] = tuple(
            Path(raw.decode("utf-8", errors="replace"))
            for raw in completed.stdout.split(b"\0")
            if raw
        )
    return discovered


def scan_repository(
    repo_root: str | Path,
    *,
    config: SecretScanConfig | None = None,
    rules: tuple[SecretRule, ...] = SECRET_RULES,
    allowlist_path: str | Path | None = None,
) -> SecretScanResult:
    root = Path(repo_root).resolve(strict=False)
    active_config = config or SecretScanConfig()
    findings: list[SecretFinding] = []
    suppressed: list[SuppressedFinding] = []
    skipped: list[SkippedFile] = []
    try:
        discovered = discover_git_files(root)
    except Exception as exc:  # noqa: BLE001 - CLI turns discovery failures into exit code 2
        return SecretScanResult(findings=(), errors=(f"{type(exc).__name__}: {exc}",))
    try:
        allowlist = load_allowlist(root, allowlist_path=allowlist_path)
    except Exception as exc:
        return SecretScanResult(findings=(), errors=(f"{type(exc).__name__}: {exc}",))

    for source_set in SOURCE_SETS:
        for relative_path in discovered.get(source_set, ()):
            findings.extend(
                _scan_path(
                    root=root,
                    relative_path=relative_path,
                    source_set=source_set,
                    config=active_config,
                    rules=rules,
                    skipped=skipped,
                )
            )
    unsuppressed: list[SecretFinding] = []
    for finding in findings:
        entry = allowlist.get((finding.path, finding.rule_id, finding.fingerprint))
        if entry is None:
            unsuppressed.append(finding)
            continue
        suppressed.append(
            SuppressedFinding(
                path=finding.path,
                line_number=finding.line_number,
                source_set=finding.source_set,
                rule_id=finding.rule_id,
                fingerprint=finding.fingerprint,
                classification=entry.classification,
                reason=entry.reason,
            )
        )
    return SecretScanResult(
        findings=tuple(unsuppressed),
        suppressed_findings=tuple(suppressed),
        skipped=tuple(skipped),
    )


def render_text(result: SecretScanResult) -> str:
    lines: list[str] = []
    for finding in result.findings:
        lines.append(
            f"{finding.path}:{finding.line_number} "
            f"{finding.rule_id} [{finding.source_set}] "
            f"{finding.fingerprint} {finding.redacted_preview}"
        )
    for suppressed in result.suppressed_findings:
        lines.append(
            f"{suppressed.path}:{suppressed.line_number} "
            f"{suppressed.rule_id} [{suppressed.source_set}] "
            f"{suppressed.fingerprint} suppressed {suppressed.classification}"
        )
    for skipped in result.skipped:
        lines.append(f"{skipped.path} skipped [{skipped.source_set}] {skipped.reason}")
    for error in result.errors:
        lines.append(f"error {error}")
    return "\n".join(lines)


def load_allowlist(
    repo_root: str | Path, *, allowlist_path: str | Path | None = None
) -> dict[tuple[str, str, str], AllowlistEntry]:
    root = Path(repo_root).resolve(strict=False)
    path = Path(allowlist_path) if allowlist_path is not None else root / _DEFAULT_ALLOWLIST
    if not path.is_absolute():
        path = root / path
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid allowlist JSON: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError("allowlist must be an object with version=1")
    raw_entries = raw.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("allowlist entries must be a list")
    entries: dict[tuple[str, str, str], AllowlistEntry] = {}
    for idx, raw_entry in enumerate(raw_entries):
        entry = _parse_allowlist_entry(raw_entry, idx)
        key = entry.key()
        if key in entries:
            raise ValueError(f"duplicate allowlist entry at index {idx}: {entry.path}")
        entries[key] = entry
    return entries


def _parse_allowlist_entry(raw: object, index: int) -> AllowlistEntry:
    if not isinstance(raw, dict):
        raise ValueError(f"allowlist entry {index} must be an object")
    required = ("path", "rule_id", "fingerprint", "classification", "reason")
    missing = [field for field in required if not str(raw.get(field, "")).strip()]
    if missing:
        raise ValueError(f"allowlist entry {index} missing required field(s): {missing}")
    path = str(raw["path"])
    rule_id = str(raw["rule_id"])
    fingerprint = str(raw["fingerprint"])
    classification = str(raw["classification"])
    reason = str(raw["reason"])
    if any(char in path for char in "*?[]"):
        raise ValueError(f"allowlist entry {index} path must be exact")
    if any(char in rule_id for char in "*?[]"):
        raise ValueError(f"allowlist entry {index} rule_id must be exact")
    if not fingerprint.startswith("sha256:") or len(fingerprint) != len("sha256:") + 64:
        raise ValueError(f"allowlist entry {index} fingerprint must be sha256")
    if classification not in _ALLOWED_SUPPRESSION_CLASSIFICATIONS:
        raise ValueError(f"allowlist entry {index} has disallowed classification")
    if rule_id in _UNSUPPRESSABLE_RULE_IDS:
        raise ValueError(f"allowlist entry {index} rule_id cannot be suppressed")
    return AllowlistEntry(
        path=path,
        rule_id=rule_id,
        fingerprint=fingerprint,
        classification=classification,
        reason=reason,
    )


def main(
    argv: Iterable[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Scan git-discovered local files for secrets.")
    parser.add_argument("--repo-root", default=".", help="Repository root to scan.")
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=_DEFAULT_MAX_FILE_BYTES,
        help="Maximum bytes to read per file before omitting it.",
    )
    parser.add_argument(
        "--allowlist",
        default=None,
        help=f"Path to allowlist JSON. Defaults to {_DEFAULT_ALLOWLIST} when present.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
        result = scan_repository(
            args.repo_root,
            config=SecretScanConfig(max_file_bytes=args.max_file_bytes),
            allowlist_path=args.allowlist,
        )
    except Exception as exc:  # noqa: BLE001 - keep CLI fail-closed and redacted
        print(f"secret scan failed: {type(exc).__name__}", file=err)
        return EXIT_ERROR

    payload = json.dumps(result.to_dict(), indent=2, sort_keys=True) if args.json else render_text(result)
    if payload:
        print(payload, file=out)
    return result.exit_code()


def _scan_path(
    *,
    root: Path,
    relative_path: Path,
    source_set: str,
    config: SecretScanConfig,
    rules: tuple[SecretRule, ...],
    skipped: list[SkippedFile],
) -> list[SecretFinding]:
    relative_text = relative_path.as_posix()
    candidate = root / relative_path
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        skipped.append(SkippedFile(relative_text, source_set, "outside_root"))
        return []
    if not resolved.is_file():
        skipped.append(SkippedFile(relative_text, source_set, "not_file"))
        return []
    try:
        size = resolved.stat().st_size
    except OSError:
        skipped.append(SkippedFile(relative_text, source_set, "stat_failed"))
        return []
    if size > config.max_file_bytes:
        skipped.append(SkippedFile(relative_text, source_set, "too_large"))
        return []
    try:
        with resolved.open("rb") as handle:
            sample = handle.read(config.binary_sample_bytes)
            if b"\0" in sample:
                skipped.append(SkippedFile(relative_text, source_set, "binary"))
                return []
            rest = handle.read(max(config.max_file_bytes - len(sample), 0))
    except OSError:
        skipped.append(SkippedFile(relative_text, source_set, "read_failed"))
        return []
    content = (sample + rest).decode("utf-8", errors="replace")
    return _scan_text(relative_text, source_set, content, rules)


def _scan_text(
    path: str,
    source_set: str,
    content: str,
    rules: tuple[SecretRule, ...],
) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        matches = _line_matches(line, rules)
        if not matches:
            continue
        spans = [span for _rule_id, _value, span in matches]
        preview = _redacted_preview(line, spans)
        for rule_id, value, _span in matches:
            findings.append(
                SecretFinding(
                    path=path,
                    line_number=line_number,
                    source_set=source_set,
                    rule_id=rule_id,
                    redacted_preview=preview,
                    fingerprint=_fingerprint(value),
                )
            )
    return findings


def _line_matches(line: str, rules: tuple[SecretRule, ...]) -> list[tuple[str, str, tuple[int, int]]]:
    found: list[tuple[str, str, tuple[int, int]]] = []
    redacted_spans: set[tuple[int, int]] = set()
    for rule in rules:
        for match in rule.pattern.finditer(line):
            value = match.group("value")
            span = match.span("value")
            if span in redacted_spans or _is_placeholder(value):
                continue
            redacted_spans.add(span)
            found.append((rule.rule_id, value, span))
    return found


def _redacted_preview(line: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return line[:200]
    parts: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        parts.append(line[cursor:start])
        parts.append(_REDACTION)
        cursor = end
    parts.append(line[cursor:])
    return "".join(parts).strip()[:240]


def _fingerprint(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().strip("\"'").lower()
    if normalized in _PLACEHOLDER_VALUES:
        return True
    if re.fullmatch(r"\$\{?[A-Z0-9_]+\}?|\{[A-Z0-9_]+\}", value.strip()):
        return True
    return False
