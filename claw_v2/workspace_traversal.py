from __future__ import annotations

import fnmatch
import os
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable


# Glob/Grep traversal skips only. This is intentionally not a security scanning
# policy; secret scanning must define its own inclusion rules.
DEFAULT_SKIP_DIRS: tuple[str, ...] = (
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "renders",
    "generated_images",
    "artifacts",
    "reports",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
)

_CHUNK_SIZE = 64 * 1024

FileFilter = Callable[[Path, Path], bool]


@dataclass(frozen=True, slots=True)
class TraversalPolicy:
    max_files: int = 5_000
    max_matches: int = 200
    max_file_bytes: int = 1_000_000
    max_total_bytes: int = 50_000_000
    deadline_ms: int = 2_000
    skip_dirs: tuple[str, ...] = DEFAULT_SKIP_DIRS
    follow_symlinks: bool = False
    binary_detection: bool = True


@dataclass(frozen=True, slots=True)
class TraversalTelemetry:
    files_scanned: int = 0
    files_skipped: int = 0
    dirs_skipped: int = 0
    bytes_scanned: int = 0
    matches_returned: int = 0
    truncated: bool = False
    deadline_exceeded: bool = False
    skipped_reasons: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "files_scanned": self.files_scanned,
            "files_skipped": self.files_skipped,
            "dirs_skipped": self.dirs_skipped,
            "bytes_scanned": self.bytes_scanned,
            "matches_returned": self.matches_returned,
            "truncated": self.truncated,
            "deadline_exceeded": self.deadline_exceeded,
            "skipped_reasons": dict(self.skipped_reasons),
        }


@dataclass(frozen=True, slots=True)
class TraversalResult:
    matches: tuple[object, ...]
    telemetry: TraversalTelemetry

    def to_dict(self) -> dict:
        return {
            "matches": list(self.matches),
            "telemetry": self.telemetry.to_dict(),
        }


@dataclass(slots=True)
class _TelemetryState:
    files_scanned: int = 0
    files_skipped: int = 0
    dirs_skipped: int = 0
    bytes_scanned: int = 0
    matches_returned: int = 0
    truncated: bool = False
    deadline_exceeded: bool = False
    skipped_reasons: dict[str, int] = field(default_factory=dict)

    def skip_file(self, reason: str) -> None:
        self.files_skipped += 1
        self.skipped_reasons[reason] = self.skipped_reasons.get(reason, 0) + 1

    def skip_dir(self, reason: str) -> None:
        self.dirs_skipped += 1
        self.skipped_reasons[reason] = self.skipped_reasons.get(reason, 0) + 1

    def truncate(self, reason: str) -> None:
        self.truncated = True
        self.skipped_reasons[reason] = self.skipped_reasons.get(reason, 0) + 1

    def snapshot(self) -> TraversalTelemetry:
        return TraversalTelemetry(
            files_scanned=self.files_scanned,
            files_skipped=self.files_skipped,
            dirs_skipped=self.dirs_skipped,
            bytes_scanned=self.bytes_scanned,
            matches_returned=self.matches_returned,
            truncated=self.truncated,
            deadline_exceeded=self.deadline_exceeded,
            skipped_reasons=dict(self.skipped_reasons),
        )


class WorkspaceTraversalService:
    def __init__(
        self,
        workspace_root: str | Path,
        *,
        policy: TraversalPolicy | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=False)
        self.policy = policy or TraversalPolicy()
        self._clock = clock or time.monotonic

    def glob_files(
        self,
        *,
        pattern: str = "**/*",
        root: str | Path | None = None,
        policy: TraversalPolicy | None = None,
        file_filter: FileFilter | None = None,
    ) -> TraversalResult:
        active_policy = policy or self.policy
        telemetry = _TelemetryState()
        matches: list[str] = []
        if active_policy.max_matches <= 0:
            telemetry.truncate("max_matches")
            return TraversalResult(matches=(), telemetry=telemetry.snapshot())
        for display_path, _read_path in self._iter_files(
            root=root,
            pattern=pattern,
            policy=active_policy,
            telemetry=telemetry,
            file_filter=file_filter,
        ):
            matches.append(str(display_path))
            telemetry.matches_returned = len(matches)
            if len(matches) >= active_policy.max_matches:
                telemetry.truncate("max_matches")
                break
        telemetry.matches_returned = len(matches)
        return TraversalResult(matches=tuple(matches), telemetry=telemetry.snapshot())

    def grep_files(
        self,
        *,
        query: str,
        pattern: str = "**/*",
        root: str | Path | None = None,
        policy: TraversalPolicy | None = None,
        file_filter: FileFilter | None = None,
        case_sensitive: bool = True,
    ) -> TraversalResult:
        active_policy = policy or self.policy
        telemetry = _TelemetryState()
        matches: list[dict] = []
        if query == "":
            return TraversalResult(matches=(), telemetry=telemetry.snapshot())
        if active_policy.max_matches <= 0:
            telemetry.truncate("max_matches")
            return TraversalResult(matches=(), telemetry=telemetry.snapshot())
        needle = query if case_sensitive else query.lower()
        for display_path, read_path in self._iter_files(
            root=root,
            pattern=pattern,
            policy=active_policy,
            telemetry=telemetry,
            file_filter=file_filter,
        ):
            if self._scan_file(
                display_path=display_path,
                read_path=read_path,
                needle=needle,
                case_sensitive=case_sensitive,
                policy=active_policy,
                telemetry=telemetry,
                matches=matches,
            ):
                break
        telemetry.matches_returned = len(matches)
        return TraversalResult(matches=tuple(matches), telemetry=telemetry.snapshot())

    def _iter_files(
        self,
        *,
        root: str | Path | None,
        pattern: str,
        policy: TraversalPolicy,
        telemetry: _TelemetryState,
        file_filter: FileFilter | None,
    ):
        start = self._clock()
        requested_root = self._resolve_requested_root(root)
        if requested_root is None:
            telemetry.skip_dir("outside_root")
            return
        stack: list[Path] = [requested_root]
        visited_dirs: set[str] = set()
        while stack:
            if self._deadline_exceeded(start, policy, telemetry):
                return
            current = stack.pop()
            if self._is_file_like(current):
                candidate = self._resolve_file_candidate(current, policy, telemetry)
                if candidate is None:
                    continue
                display_path, read_path = candidate
                if not self._count_file(policy, telemetry):
                    return
                if not self._matches_pattern(display_path, pattern):
                    continue
                if file_filter is not None and not self._file_filter_allows(
                    file_filter, display_path, read_path, telemetry
                ):
                    continue
                yield display_path, read_path
                continue

            if not self._is_directory_like(current):
                telemetry.skip_file("not_file")
                continue
            if current.name in policy.skip_dirs:
                telemetry.skip_dir("skip_dir")
                continue
            if current.is_symlink() and not policy.follow_symlinks:
                telemetry.skip_dir("symlink_dir")
                continue
            current_real = current.resolve(strict=False)
            if not self._is_inside_root(current_real):
                telemetry.skip_dir("outside_root")
                continue
            current_key = str(current_real)
            if current_key in visited_dirs:
                telemetry.skip_dir("cycle")
                continue
            visited_dirs.add(current_key)
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if self._deadline_exceeded(start, policy, telemetry):
                            return
                        child = Path(entry.path)
                        if entry.is_symlink():
                            resolved = child.resolve(strict=False)
                            if not self._is_inside_root(resolved):
                                if resolved.is_dir():
                                    telemetry.skip_dir("outside_root")
                                else:
                                    telemetry.skip_file("outside_root")
                                continue
                            if resolved.is_dir():
                                if policy.follow_symlinks:
                                    stack.append(child)
                                else:
                                    telemetry.skip_dir("symlink_dir")
                                continue
                            if not self._count_file(policy, telemetry):
                                return
                            if self._matches_pattern(child, pattern):
                                if file_filter is not None and not self._file_filter_allows(
                                    file_filter, child, resolved, telemetry
                                ):
                                    continue
                                yield child, resolved
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if child.name in policy.skip_dirs:
                                telemetry.skip_dir("skip_dir")
                                continue
                            stack.append(child)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            telemetry.skip_file("not_file")
                            continue
                        if not self._count_file(policy, telemetry):
                            return
                        if self._matches_pattern(child, pattern):
                            if file_filter is not None and not self._file_filter_allows(
                                file_filter, child, child, telemetry
                            ):
                                continue
                            yield child, child
            except OSError:
                telemetry.skip_dir("os_error")

    def _scan_file(
        self,
        *,
        display_path: Path,
        read_path: Path,
        needle: str,
        case_sensitive: bool,
        policy: TraversalPolicy,
        telemetry: _TelemetryState,
        matches: list[dict],
    ) -> bool:
        file_bytes = 0
        line_number = 1
        pending = b""
        checked_binary = False
        try:
            with read_path.open("rb") as handle:
                while True:
                    if telemetry.bytes_scanned >= policy.max_total_bytes:
                        self._flush_pending_line(
                            display_path=display_path,
                            pending=pending,
                            line_number=line_number,
                            needle=needle,
                            case_sensitive=case_sensitive,
                            matches=matches,
                            policy=policy,
                        )
                        telemetry.matches_returned = len(matches)
                        telemetry.truncate("max_total_bytes")
                        return True
                    if file_bytes >= policy.max_file_bytes:
                        self._flush_pending_line(
                            display_path=display_path,
                            pending=pending,
                            line_number=line_number,
                            needle=needle,
                            case_sensitive=case_sensitive,
                            matches=matches,
                            policy=policy,
                        )
                        telemetry.matches_returned = len(matches)
                        telemetry.truncate("max_file_bytes")
                        return False
                    to_read = min(
                        _CHUNK_SIZE,
                        policy.max_file_bytes - file_bytes,
                        policy.max_total_bytes - telemetry.bytes_scanned,
                    )
                    if to_read <= 0:
                        self._flush_pending_line(
                            display_path=display_path,
                            pending=pending,
                            line_number=line_number,
                            needle=needle,
                            case_sensitive=case_sensitive,
                            matches=matches,
                            policy=policy,
                        )
                        telemetry.matches_returned = len(matches)
                        telemetry.truncate("budget_exhausted")
                        return True
                    chunk = handle.read(to_read)
                    if not chunk:
                        break
                    if policy.binary_detection and not checked_binary:
                        checked_binary = True
                        if self._looks_binary(chunk[:4096]):
                            telemetry.skip_file("binary")
                            return False
                    file_bytes += len(chunk)
                    telemetry.bytes_scanned += len(chunk)
                    pending, stop = self._process_chunk(
                        display_path=display_path,
                        data=pending + chunk,
                        needle=needle,
                        case_sensitive=case_sensitive,
                        line_number=line_number,
                        matches=matches,
                        policy=policy,
                        telemetry=telemetry,
                    )
                    line_number += stop
                    if len(matches) >= policy.max_matches:
                        telemetry.matches_returned = len(matches)
                        telemetry.truncate("max_matches")
                        return True
                if pending:
                    self._append_line_match(
                        display_path=display_path,
                        raw_line=pending,
                        line_number=line_number,
                        needle=needle,
                        case_sensitive=case_sensitive,
                        matches=matches,
                        policy=policy,
                    )
                    if len(matches) >= policy.max_matches:
                        telemetry.matches_returned = len(matches)
                        telemetry.truncate("max_matches")
                        return True
        except UnicodeDecodeError:
            telemetry.skip_file("unicode_decode_error")
        except OSError:
            telemetry.skip_file("os_error")
        return False

    def _process_chunk(
        self,
        *,
        display_path: Path,
        data: bytes,
        needle: str,
        case_sensitive: bool,
        line_number: int,
        matches: list[dict],
        policy: TraversalPolicy,
        telemetry: _TelemetryState,
    ) -> tuple[bytes, int]:
        raw_lines = data.splitlines(keepends=True)
        pending = b""
        if raw_lines and not raw_lines[-1].endswith((b"\n", b"\r")):
            pending = raw_lines.pop()
        processed = 0
        for raw_line in raw_lines:
            self._append_line_match(
                display_path=display_path,
                raw_line=raw_line.rstrip(b"\r\n"),
                line_number=line_number + processed,
                needle=needle,
                case_sensitive=case_sensitive,
                matches=matches,
                policy=policy,
            )
            processed += 1
            if len(matches) >= policy.max_matches:
                telemetry.matches_returned = len(matches)
                break
        return pending, processed

    def _append_line_match(
        self,
        *,
        display_path: Path,
        raw_line: bytes,
        line_number: int,
        needle: str,
        case_sensitive: bool,
        matches: list[dict],
        policy: TraversalPolicy,
    ) -> None:
        if len(matches) >= policy.max_matches:
            return
        line = raw_line.decode("utf-8", errors="replace")
        haystack = line if case_sensitive else line.lower()
        if needle in haystack:
            matches.append(
                {"path": str(display_path), "line_number": line_number, "line": line}
            )

    def _flush_pending_line(
        self,
        *,
        display_path: Path,
        pending: bytes,
        line_number: int,
        needle: str,
        case_sensitive: bool,
        matches: list[dict],
        policy: TraversalPolicy,
    ) -> None:
        if not pending:
            return
        self._append_line_match(
            display_path=display_path,
            raw_line=pending,
            line_number=line_number,
            needle=needle,
            case_sensitive=case_sensitive,
            matches=matches,
            policy=policy,
        )

    def _resolve_requested_root(self, root: str | Path | None) -> Path | None:
        raw_root = self.workspace_root if root is None else Path(root)
        candidate = raw_root if raw_root.is_absolute() else self.workspace_root / raw_root
        resolved = candidate.resolve(strict=False)
        if not self._is_inside_root(resolved):
            return None
        return resolved

    def _resolve_file_candidate(
        self, path: Path, policy: TraversalPolicy, telemetry: _TelemetryState
    ) -> tuple[Path, Path] | None:
        if path.is_symlink():
            resolved = path.resolve(strict=False)
            if not self._is_inside_root(resolved):
                telemetry.skip_file("outside_root")
                return None
            if resolved.is_dir():
                if policy.follow_symlinks:
                    return None
                telemetry.skip_dir("symlink_dir")
                return None
            return path, resolved
        if not self._is_inside_root(path.resolve(strict=False)):
            telemetry.skip_file("outside_root")
            return None
        return path, path

    def _is_file_like(self, path: Path) -> bool:
        try:
            return path.is_file() or (path.is_symlink() and path.resolve(strict=False).is_file())
        except OSError:
            return False

    def _is_directory_like(self, path: Path) -> bool:
        try:
            return path.is_dir() or (path.is_symlink() and path.resolve(strict=False).is_dir())
        except OSError:
            return False

    def _file_filter_allows(
        self,
        file_filter: FileFilter,
        display_path: Path,
        read_path: Path,
        telemetry: _TelemetryState,
    ) -> bool:
        try:
            allowed = file_filter(display_path, read_path)
        except PermissionError:
            allowed = False
        if not allowed:
            telemetry.skip_file("policy_denied")
            return False
        return True

    def _count_file(self, policy: TraversalPolicy, telemetry: _TelemetryState) -> bool:
        if telemetry.files_scanned >= policy.max_files:
            telemetry.truncate("max_files")
            return False
        telemetry.files_scanned += 1
        return True

    def _deadline_exceeded(
        self, start: float, policy: TraversalPolicy, telemetry: _TelemetryState
    ) -> bool:
        if policy.deadline_ms < 0:
            return False
        if (self._clock() - start) * 1000 <= policy.deadline_ms:
            return False
        telemetry.deadline_exceeded = True
        telemetry.truncate("deadline")
        return True

    def _matches_pattern(self, path: Path, pattern: str) -> bool:
        rel = self._relative_posix(path)
        name = path.name
        normalized = pattern or "**/*"
        candidates = [normalized]
        if normalized.startswith("**/"):
            candidates.append(normalized[3:])
        rel_path = PurePosixPath(rel)
        return any(
            rel_path.match(candidate)
            or fnmatch.fnmatchcase(rel, candidate)
            or fnmatch.fnmatchcase(name, candidate)
            for candidate in candidates
        )

    def _relative_posix(self, path: Path) -> str:
        try:
            return path.relative_to(self.workspace_root).as_posix()
        except ValueError:
            try:
                return path.resolve(strict=False).relative_to(self.workspace_root).as_posix()
            except ValueError:
                return path.as_posix()

    def _is_inside_root(self, path: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(self.workspace_root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _looks_binary(sample: bytes) -> bool:
        if b"\0" in sample:
            return True
        return False
