from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


RenderMode = Literal["normal", "debug", "audit"]


_INTERNAL_LABEL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("approval_id", re.compile(r"\bapproval_id\b|Approval ID", re.IGNORECASE)),
    (
        "imperative_receipt",
        re.compile(
            r"^\s*(?:Intent|Target|Artifact|Estado|Task|Resultado|Capability faltante)\s*:",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    ("task.contextual_action", re.compile(r"\btask\.contextual_action\b", re.IGNORECASE)),
    ("needs_approval", re.compile(r"\bneeds_approval\b|\bpending_approval\b", re.IGNORECASE)),
    ("waiting_for_user_input", re.compile(r"\bwaiting_for_user_input\b", re.IGNORECASE)),
    ("explicit_blocker", re.compile(r"\bexplicit_blocker\b", re.IGNORECASE)),
    ("risk_high", re.compile(r"\brisk[_ -]?tier\s*[:=]?\s*`?high`?\b|\brisk\s*[:=]?\s*`?high`?\b", re.IGNORECASE)),
    ("internal_command", re.compile(r"/(?:task_approve|task_abort|action_approve|action_abort|approve|approval_status)\b", re.IGNORECASE)),
)

_LINE_DROP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bapproval_id\b\s*[:=]", re.IGNORECASE),
    re.compile(
        r"^\s*(?:Intent|Target|Artifact|Estado|Task|Resultado|Capability faltante)\s*:",
        re.IGNORECASE,
    ),
    re.compile(r"\bApprove via\b|\bAbort via\b", re.IGNORECASE),
    re.compile(r"^\s*(?:Comando|Command)\s*:", re.IGNORECASE),
    re.compile(r"/(?:task_approve|task_abort|action_approve|action_abort|approve|approval_status)\b", re.IGNORECASE),
    re.compile(r"^\s*(?:\*\*)?\s*pido\s+tu\s+ok\s+antes\s+de\s+tocar\b.*$", re.IGNORECASE),
    re.compile(r"^\s*una\s+palabra\s*:\s*`?(?:dale|todo)`?\b.*$", re.IGNORECASE),
)
_SECTION_DROP_HEADING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*checkpoint\s*:?\s*(?:\*\*)?\s*$", re.IGNORECASE),
)

_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\btask\.contextual_action\b", re.IGNORECASE), "la acción contextual"),
    (re.compile(r"\bneeds_approval\b|\bpending_approval\b", re.IGNORECASE), "pendiente de autorización"),
    (re.compile(r"\bwaiting_for_user_input\b", re.IGNORECASE), "esperando tu respuesta"),
    (re.compile(r"\bexplicit_blocker\b", re.IGNORECASE), "bloqueo verificado"),
    (re.compile(r"\brisk[_ -]?tier\s*[:=]?\s*`?high`?\b", re.IGNORECASE), "riesgo alto"),
)


@dataclass(slots=True)
class NaturalLanguageRenderer:
    """Small final-pass renderer for user-visible text.

    It keeps IDs and internal routing labels available in debug/audit modes,
    but removes them from normal chat copy. This does not decide policy; it
    only controls presentation.
    """

    mode: RenderMode = "normal"

    def leaked_internal_labels(self, text: str) -> list[str]:
        if not text:
            return []
        labels: list[str] = []
        for label, pattern in _INTERNAL_LABEL_PATTERNS:
            if pattern.search(text) and label not in labels:
                labels.append(label)
        return labels

    def render(self, text: str) -> str:
        if self.mode in {"debug", "audit"}:
            return text
        if not text:
            return text
        lines: list[str] = []
        dropping_section = False
        for line in str(text).splitlines():
            if dropping_section:
                if not line.strip():
                    dropping_section = False
                elif re.match(r"^\s*#{1,6}\s+\S", line):
                    dropping_section = False
                else:
                    continue
            if any(pattern.search(line) for pattern in _SECTION_DROP_HEADING_PATTERNS):
                dropping_section = True
                continue
            if any(pattern.search(line) for pattern in _LINE_DROP_PATTERNS):
                continue
            lines.append(line)
        rendered = "\n".join(lines).strip()
        for pattern, replacement in _REPLACEMENTS:
            rendered = pattern.sub(replacement, rendered)
        rendered = re.sub(r"\n{3,}", "\n\n", rendered).strip()
        return rendered or "Listo. Tengo el siguiente paso identificado; dime si procedo."
