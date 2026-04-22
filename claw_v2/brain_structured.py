from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from claw_v2.brain_json import _strip_trace_tags, _try_parse_json_object, _validate_schema_keys
from claw_v2.memory import MemoryStore
from claw_v2.types import LLMResponse

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StructuredResponseService:
    memory: MemoryStore
    handle_message: Callable[..., LLMResponse]

    def handle(
        self,
        session_id: str,
        message: str,
        *,
        schema: dict[str, Any],
        task_type: str | None = None,
        store_history: bool = True,
        max_retries: int = 1,
    ) -> dict[str, Any]:
        schema_text = json.dumps(schema, indent=2)
        instruction = _structured_instruction(schema_text, message, retry=False)
        last_content = ""
        for attempt in range(1 + max_retries):
            if attempt > 0:
                instruction = _structured_instruction(schema_text, message, retry=True)
            response = self.handle_message(session_id, instruction, task_type=task_type)
            last_content = _strip_trace_tags(response.content.strip())
            parsed = _try_parse_json_object(last_content)
            if parsed is not None:
                errors = _validate_schema_keys(parsed, schema)
                if errors:
                    logger.debug("Schema validation issues (non-fatal): %s", errors)
                if not store_history:
                    self.memory.delete_last_messages(session_id, count=2 * (attempt + 1))
                return parsed

        if not store_history:
            self.memory.delete_last_messages(session_id, count=2 * (1 + max_retries))
        return {"raw": last_content}


def _structured_instruction(schema_text: str, message: str, *, retry: bool) -> str:
    if retry:
        return (
            "Your previous response was not valid JSON. "
            "Respond with ONLY the JSON object wrapped in <response> tags, nothing else.\n\n"
            f"Schema:\n```json\n{schema_text}\n```\n\n"
            f"Task: {message}"
        )
    return (
        "Respond with valid JSON matching this schema, wrapped in <response> tags "
        "(no markdown fences and no text outside <response>):\n"
        f"```json\n{schema_text}\n```\n\n"
        f"Task: {message}"
    )
