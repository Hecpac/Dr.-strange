from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable

from claw_v2.redaction import redact_text


PACKAGE_MANIFESTS = frozenset(
    {
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "pipfile",
        "gemfile",
        "cargo.toml",
        "go.mod",
        "pom.xml",
    }
)
LOCKFILES = frozenset(
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


@dataclass(frozen=True, slots=True)
class SensitiveChangeClassification:
    sensitive: bool
    categories: tuple[str, ...]
    sensitive_paths: tuple[str, ...]
    diff_summary: str
    risk_code: str | None
    required_confirmation: str | None
    risk_basis: str | None


def classify_sensitive_change(
    *,
    diff: str | None = None,
    paths: Iterable[str] | None = None,
    action: str = "",
    summary: str = "",
) -> SensitiveChangeClassification:
    """Classify approval risk from changed paths and compact redacted diff text."""

    explicit_paths = {_normalize_path(p) for p in (paths or ()) if p}
    all_paths = sorted(_paths_from_diff(diff or "") | explicit_paths)
    path_categories: dict[str, set[str]] = {}
    categories: set[str] = set()
    for path in all_paths:
        matched = _categories_for_path(path)
        if matched:
            path_categories[path] = matched
            categories.update(matched)
    sensitive_paths = tuple(path_categories)
    ordered_categories = tuple(sorted(categories))
    diff_summary = summarize_diff(diff or "", fallback_paths=all_paths)
    if not sensitive_paths:
        return SensitiveChangeClassification(
            sensitive=False,
            categories=(),
            sensitive_paths=(),
            diff_summary=diff_summary,
            risk_code=None,
            required_confirmation=None,
            risk_basis=None,
        )
    risk_code = _risk_code(
        action=action, summary=summary, paths=sensitive_paths, categories=ordered_categories
    )
    return SensitiveChangeClassification(
        sensitive=True,
        categories=ordered_categories,
        sensitive_paths=sensitive_paths,
        diff_summary=diff_summary,
        risk_code=risk_code,
        required_confirmation=f"CONFIRMO {risk_code}",
        risk_basis=f"sensitive_change:{','.join(ordered_categories)}",
    )


def approval_metadata_for_change(
    *,
    metadata: dict | None = None,
    action: str = "",
    summary: str = "",
    diff: str | None = None,
    paths: Iterable[str] | None = None,
) -> tuple[dict, SensitiveChangeClassification]:
    merged = dict(metadata or {})
    explicit_paths = (
        merged.get("changed_paths") or merged.get("paths") or merged.get("sensitive_paths") or ()
    )
    if isinstance(explicit_paths, str):
        explicit_paths = (explicit_paths,)
    classification = classify_sensitive_change(
        diff=diff,
        paths=tuple(paths or ()) + tuple(explicit_paths or ()),
        action=action,
        summary=summary,
    )
    merged.setdefault("diff_summary", classification.diff_summary)
    merged.setdefault("sensitive_paths", list(classification.sensitive_paths))
    merged.setdefault("risk_code", classification.risk_code)
    merged.setdefault("required_confirmation", classification.required_confirmation)
    if classification.categories:
        merged.setdefault("sensitive_categories", list(classification.categories))
    return merged, classification


def summarize_diff(diff: str, *, fallback_paths: Iterable[str] = (), max_chars: int = 1200) -> str:
    if diff.strip():
        lines: list[str] = []
        for line in diff.splitlines():
            if line.startswith("diff --git ") or line.startswith(("+++", "---", "@@")):
                lines.append(line)
            elif line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                lines.append(line)
            if len(lines) >= 80:
                break
        return redact_text("\n".join(lines), limit=max_chars)
    path_text = "\n".join(sorted(_normalize_path(p) for p in fallback_paths if p))
    return redact_text(path_text, limit=max_chars)


def _paths_from_diff(diff: str) -> set[str]:
    paths: set[str] = set()
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            for raw in parts[2:4]:
                normalized = _strip_git_prefix(raw)
                if normalized:
                    paths.add(normalized)
        elif line.startswith(("+++ ", "--- ")):
            raw = line.split(maxsplit=1)[1]
            normalized = _strip_git_prefix(raw)
            if normalized and normalized != "/dev/null":
                paths.add(normalized)
    return paths


def _strip_git_prefix(value: str) -> str:
    value = value.strip()
    if value in {"/dev/null", "dev/null"}:
        return ""
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return _normalize_path(value)


def _normalize_path(path: str) -> str:
    return str(path).replace("\\", "/").strip().lstrip("./")


def _categories_for_path(path: str) -> set[str]:
    lowered = path.lower()
    name = PurePosixPath(lowered).name
    categories: set[str] = set()
    if name.startswith(".env") or "/.env" in lowered:
        categories.add(".env")
    if name in PACKAGE_MANIFESTS or _looks_like_requirements_file(name):
        categories.add("package_manifest")
        categories.add("deps")
    if name in LOCKFILES:
        categories.add("lockfile")
        categories.add("deps")
    if re.search(
        r"(^|/)(auth|oauth|login|session|credential|credentials|permission|permissions)(/|_|-|\.|$)",
        lowered,
    ):
        categories.add("auth")
    if re.search(r"(^|/)(crypto|cryptography|cipher|tls|ssl|hmac|jwt)(/|_|-|\.|$)", lowered):
        categories.add("crypto")
    if "sandbox" in lowered or name in {"container.py", "terminal_bridge.py"}:
        categories.add("sandbox")
    if name in {"runtime_policy.py", "execution_environment.py"}:
        categories.add("runtime_policy")
    if "approval" in lowered:
        categories.add("approval")
    if "telegram" in lowered or "imperative_router" in lowered or "dispatch_routing" in lowered:
        categories.add("telegram_routing")
    if name in {"bot.py", "bot_helpers.py"}:
        categories.add("telegram_routing")
    if (
        name in {"config.py", "settings.py"}
        or "/config/" in lowered
        or lowered.endswith((".toml", ".yaml", ".yml", ".ini"))
    ):
        categories.add("config")
    return categories


def _looks_like_requirements_file(name: str) -> bool:
    return name.startswith("requirements") and name.endswith(".txt")


def _risk_code(
    *, action: str, summary: str, paths: tuple[str, ...], categories: tuple[str, ...]
) -> str:
    material = "\n".join([action, summary, *categories, *paths])
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:8].upper()
    return f"RISK-{digest}"
