from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable

from claw_v2.redaction import redact_text
from claw_v2.runtime_policy import sanitize_child_env

logger = logging.getLogger(__name__)

Runner = Callable[..., subprocess.CompletedProcess[str]]

_SOURCE_ID_RE = re.compile(r"Source ID:\s*(?P<id>\S+)", re.IGNORECASE)
_ADDED_SOURCE_RE = re.compile(r"Added source:\s*(?P<title>.+?)(?:\s+\(ready\))?$", re.IGNORECASE)
_COUNT_RE = re.compile(r"\b(?P<count>\d+)\s+source(?:\(s\)|s)?\b", re.IGNORECASE)


class NotebookLMAdapterError(RuntimeError):
    """Raised when an external NotebookLM backend cannot complete a request."""


@dataclass(slots=True)
class JacobNotebookLMCLIAdapter:
    """Adapter for jacob-bd/notebooklm-mcp-cli's `nlm` command.

    The agent keeps its existing NotebookLM service contract and calls this
    adapter only as a backend. This avoids exposing the package's broad MCP
    tool surface directly to the brain.
    """

    command: str = "nlm"
    profile: str | None = None
    timeout_seconds: float = 120.0
    long_timeout_seconds: float = 1200.0
    artifact_timeout_seconds: float = 1200.0
    poll_interval_seconds: float = 20.0
    runner: Runner | None = None

    def list_notebooks(self) -> list[dict[str, Any]]:
        completed = self._run(["notebook", "list", "--json", "--full"])
        data = self._parse_json(completed.stdout)
        if not isinstance(data, list):
            raise NotebookLMAdapterError("nlm notebook list returned non-list JSON")
        notebooks = []
        for item in data:
            if not isinstance(item, dict):
                continue
            notebook_id = str(item.get("id") or item.get("notebook_id") or "").strip()
            if not notebook_id:
                continue
            notebooks.append(
                {
                    "id": notebook_id,
                    "title": str(item.get("title") or notebook_id[:8]),
                    "created_at": item.get("created_at") or item.get("updated_at") or "",
                    "source_count": int(item.get("source_count") or item.get("sources_count") or 0),
                }
            )
        return notebooks

    def create_notebook(self, title: str) -> dict[str, str]:
        completed = self._run(["notebook", "create", title, "--json"])
        data = self._parse_json(completed.stdout)
        if not isinstance(data, dict):
            raise NotebookLMAdapterError("nlm notebook create returned non-object JSON")
        notebook_id = str(data.get("notebook_id") or data.get("id") or "").strip()
        if not notebook_id:
            raise NotebookLMAdapterError("nlm notebook create returned no notebook id")
        return {"id": notebook_id, "title": str(data.get("title") or title)}

    def delete_notebook(self, notebook_id: str) -> bool:
        self._run(["notebook", "delete", notebook_id, "--confirm"])
        return True

    def status(self, notebook_id: str) -> dict[str, Any]:
        data = self._notebook_details(notebook_id)
        normalized_id = str(data.get("notebook_id") or data.get("id") or notebook_id)
        sources = []
        for source in data.get("sources") or []:
            if not isinstance(source, dict):
                continue
            sources.append(
                {
                    "id": str(source.get("id") or ""),
                    "title": str(source.get("title") or ""),
                    "kind": str(source.get("kind") or source.get("type") or ""),
                    "url": str(source.get("url") or ""),
                }
            )
        return {
            "notebook": {
                "id": normalized_id,
                "title": str(data.get("title") or normalized_id[:8]),
                "sources_count": int(data.get("source_count") or len(sources)),
            },
            "sources": sources,
        }

    def add_sources(self, notebook_id: str, urls: list[str]) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        for url in urls:
            completed = self._run(
                [
                    "source",
                    "add",
                    notebook_id,
                    "--url",
                    url,
                    "--wait",
                    "--wait-timeout",
                    str(int(self.long_timeout_seconds)),
                ],
                timeout=self.long_timeout_seconds,
            )
            results.append(self._parse_source_add_output(completed.stdout, fallback_title=url))
        return results

    def add_text(self, notebook_id: str, title: str, content: str) -> dict[str, str]:
        completed = self._run(
            [
                "source",
                "add",
                notebook_id,
                "--text",
                content,
                "--title",
                title,
                "--wait",
                "--wait-timeout",
                str(int(self.long_timeout_seconds)),
            ],
            timeout=self.long_timeout_seconds,
        )
        return self._parse_source_add_output(completed.stdout, fallback_title=title)

    def chat(self, notebook_id: str, question: str) -> str:
        completed = self._run(
            [
                "notebook",
                "query",
                notebook_id,
                question,
                "--json",
                "--timeout",
                str(int(self.timeout_seconds)),
            ],
            timeout=self.timeout_seconds + 30,
        )
        data = self._parse_json(completed.stdout)
        if isinstance(data, dict):
            return str(data.get("answer") or data.get("response") or "")
        raise NotebookLMAdapterError("nlm notebook query returned non-object JSON")

    def deep_research(self, notebook_id: str, query: str, mode: str = "deep") -> int:
        normalized_mode = mode if mode in {"fast", "deep"} else "deep"
        before = self._source_count_or_none(notebook_id)
        completed = self._run(
            [
                "research",
                "start",
                query,
                "--notebook-id",
                notebook_id,
                "--source",
                "web",
                "--mode",
                normalized_mode,
                "--auto-import",
                "--force",
            ],
            timeout=self.long_timeout_seconds,
        )
        after = self._source_count_or_none(notebook_id)
        parsed = _max_source_count(completed.stdout)
        if before is not None and after is not None:
            return max(after - before, parsed or 0, 0)
        return parsed or 0

    def generate_artifact(self, notebook_id: str, kind: str) -> None:
        normalized_kind = kind.strip().lower()
        artifact_type = {
            "podcast": "audio",
            "infographic": "infographic",
            "video": "video",
        }.get(normalized_kind)
        if artifact_type is None:
            raise NotebookLMAdapterError(f"Unsupported artifact kind: {kind}")

        before_ids = {artifact["id"] for artifact in self._list_artifacts(notebook_id) if artifact.get("id")}
        command = {
            "podcast": ["audio", "create", notebook_id, "--confirm"],
            "infographic": ["infographic", "create", notebook_id, "--confirm"],
            "video": ["video", "create", notebook_id, "--confirm"],
        }[normalized_kind]
        self._run(command, timeout=self.timeout_seconds)

        deadline = time.monotonic() + self.artifact_timeout_seconds
        last_status = "unknown"
        while time.monotonic() < deadline:
            artifacts = self._list_artifacts(notebook_id)
            candidates = [
                artifact
                for artifact in artifacts
                if _artifact_type_matches(str(artifact.get("type") or ""), artifact_type)
                and (not before_ids or artifact.get("id") not in before_ids)
            ]
            if not candidates and not before_ids:
                candidates = [
                    artifact
                    for artifact in artifacts
                    if _artifact_type_matches(str(artifact.get("type") or ""), artifact_type)
                ]
            for artifact in candidates:
                status = str(artifact.get("status") or "").lower()
                last_status = status or last_status
                if status == "completed":
                    return
                if status == "failed":
                    raise NotebookLMAdapterError(f"nlm {artifact_type} generation failed")
            time.sleep(max(1.0, self.poll_interval_seconds))
        raise TimeoutError(f"nlm {artifact_type} generation did not complete; last_status={last_status}")

    def _notebook_details(self, notebook_id: str) -> dict[str, Any]:
        completed = self._run(["notebook", "get", notebook_id, "--json"])
        data = self._parse_json(completed.stdout)
        if not isinstance(data, dict):
            raise NotebookLMAdapterError("nlm notebook get returned non-object JSON")
        return data

    def _list_artifacts(self, notebook_id: str) -> list[dict[str, Any]]:
        completed = self._run(["studio", "status", notebook_id, "--json", "--full"])
        data = self._parse_json(completed.stdout)
        if not isinstance(data, list):
            raise NotebookLMAdapterError("nlm studio status returned non-list JSON")
        return [item for item in data if isinstance(item, dict)]

    def _source_count_or_none(self, notebook_id: str) -> int | None:
        try:
            data = self._notebook_details(notebook_id)
        except Exception as exc:
            logger.debug("Could not read NotebookLM source count via nlm: %s", exc)
            return None
        try:
            return int(data.get("source_count") or 0)
        except (TypeError, ValueError):
            return None

    def _parse_source_add_output(self, output: str, *, fallback_title: str) -> dict[str, str]:
        source_id = ""
        title = fallback_title
        for line in output.splitlines():
            id_match = _SOURCE_ID_RE.search(line)
            if id_match:
                source_id = id_match.group("id")
            added_match = _ADDED_SOURCE_RE.search(line)
            if added_match:
                title = added_match.group("title").strip()
        return {"id": source_id, "title": title}

    def _run(
        self,
        args: list[str],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [self.command, *args, *self._profile_args()]
        runner = self.runner or subprocess.run
        if self.runner is None and shutil.which(self.command) is None:
            raise NotebookLMAdapterError(
                f"NotebookLM CLI command not found: {self.command}. "
                "Install jacob-bd/notebooklm-mcp-cli with `uv tool install notebooklm-mcp-cli`."
            )
        env_result = sanitize_child_env()
        completed = runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout or self.timeout_seconds,
            check=False,
            stdin=subprocess.DEVNULL,
            env=env_result.env,
        )
        if completed.returncode != 0:
            detail = redact_text((completed.stderr or completed.stdout or "").strip(), limit=1200)
            raise NotebookLMAdapterError(
                f"nlm command failed ({completed.returncode}) for {_safe_command_label(command)}: {detail}"
            )
        return completed

    def _profile_args(self) -> list[str]:
        profile = (self.profile or "").strip()
        return ["--profile", profile] if profile else []

    def _parse_json(self, text: str) -> Any:
        stripped = (text or "").strip()
        if not stripped:
            raise NotebookLMAdapterError("nlm returned empty output")
        decoder = json.JSONDecoder()
        starts = sorted(index for index in (stripped.find("{"), stripped.find("[")) if index >= 0)
        for start in starts:
            try:
                data, _ = decoder.raw_decode(stripped[start:])
                return data
            except json.JSONDecodeError:
                continue
        raise NotebookLMAdapterError(
            "nlm returned non-JSON output: " + redact_text(stripped, limit=500)
        )


def _safe_command_label(command: list[str]) -> str:
    redacted = []
    skip_next = False
    for token in command:
        if skip_next:
            redacted.append("REDACTED")
            skip_next = False
            continue
        if token in {"--profile"}:
            redacted.append(token)
            skip_next = True
            continue
        redacted.append(redact_text(token, limit=120))
    return " ".join(redacted[:8])


def _max_source_count(text: str) -> int | None:
    counts = []
    for match in _COUNT_RE.finditer(text or ""):
        try:
            counts.append(int(match.group("count")))
        except (TypeError, ValueError):
            continue
    return max(counts) if counts else None


def _artifact_type_matches(value: str, expected: str) -> bool:
    normalized = value.strip().lower().replace("_", "-")
    return normalized == expected or normalized.replace("-", "_") == expected.replace("-", "_")
