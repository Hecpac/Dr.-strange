from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from claw_v2.adapters.base import AdapterError, UserPrompt
from claw_v2.brain_response import _brain_system_prompt, _extract_visible_brain_response, _summarize_user_prompt
from claw_v2.llm import LLMRouter
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.tracing import attach_trace, child_trace_context, new_trace_context
from claw_v2.types import LLMResponse

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BrainTurnService:
    router: LLMRouter
    memory: MemoryStore
    system_prompt: str
    build_prompt: Callable[..., UserPrompt]
    observe: ObserveStream | None = None

    def handle_message(
        self,
        session_id: str,
        message: UserPrompt,
        *,
        memory_text: str | None = None,
        task_type: str | None = None,
    ) -> LLMResponse:
        stored_user_message = memory_text or _summarize_user_prompt(message)
        trace = new_trace_context(artifact_id=session_id)
        provider_session_id = self.memory.get_provider_session(session_id, "anthropic")
        provider_cursor = self.memory.get_provider_session_cursor(session_id, "anthropic")
        resuming = provider_session_id is not None
        prompt = self.build_prompt(
            session_id=session_id,
            message=message,
            stored_user_message=stored_user_message,
            include_history=not resuming,
            catchup_after_id=provider_cursor,
            task_type=task_type,
        )
        try:
            self._emit_turn_start(trace, session_id)
            response = self.router.ask(
                prompt,
                system_prompt=_brain_system_prompt(self.system_prompt),
                lane="brain",
                session_id=provider_session_id,
                evidence_pack=attach_trace({"app_session_id": session_id}, trace),
                max_budget=2.0,
                timeout=300.0,
            )
        except AdapterError:
            if not resuming:
                raise
            logger.warning("Session resume failed for %s, retrying with fresh session", session_id)
            self._emit_session_resume_failed(trace, session_id, provider_session_id)
            self.memory.clear_provider_session(session_id, "anthropic")
            prompt = self.build_prompt(
                session_id=session_id,
                message=message,
                stored_user_message=stored_user_message,
                include_history=True,
                catchup_after_id=None,
                task_type=task_type,
            )
            response = self.router.ask(
                prompt,
                system_prompt=_brain_system_prompt(self.system_prompt),
                lane="brain",
                session_id=None,
                evidence_pack=attach_trace({"app_session_id": session_id}, trace),
                max_budget=2.0,
                timeout=300.0,
            )
        response = _extract_visible_brain_response(response)
        self._store_turn(session_id, stored_user_message, response)
        self._emit_turn_complete(trace, session_id, response)
        return response

    def _emit_turn_start(self, trace: dict[str, Any], session_id: str) -> None:
        if self.observe is None:
            return
        self.observe.emit(
            "brain_turn_start",
            trace_id=trace["trace_id"],
            root_trace_id=trace["root_trace_id"],
            span_id=trace["span_id"],
            parent_span_id=trace["parent_span_id"],
            artifact_id=trace["artifact_id"],
            payload={"app_session_id": session_id},
        )

    def _emit_session_resume_failed(self, trace: dict[str, Any], session_id: str, stale_session: str | None) -> None:
        if self.observe is None:
            return
        self.observe.emit(
            "session_resume_failed",
            lane="brain",
            provider="anthropic",
            trace_id=trace["trace_id"],
            root_trace_id=trace["root_trace_id"],
            span_id=trace["span_id"],
            parent_span_id=trace["parent_span_id"],
            artifact_id=trace["artifact_id"],
            payload={"app_session_id": session_id, "stale_session": stale_session},
        )

    def _store_turn(self, session_id: str, user_message: str, response: LLMResponse) -> None:
        provider_session_artifact = response.artifacts.get("session_id")
        self.memory.store_message(session_id, "user", user_message)
        self.memory.store_message(session_id, "assistant", response.content)
        if isinstance(provider_session_artifact, str) and provider_session_artifact:
            self.memory.link_provider_session(
                session_id,
                response.provider,
                provider_session_artifact,
                last_message_id=self.memory.last_message_id(session_id),
            )

    def _emit_turn_complete(self, trace: dict[str, Any], session_id: str, response: LLMResponse) -> None:
        if self.observe is None:
            return
        completion = child_trace_context(trace, artifact_id=session_id)
        reasoning_trace = response.artifacts.get("reasoning_trace")
        if isinstance(reasoning_trace, str) and reasoning_trace.strip():
            self.observe.emit(
                "brain_reasoning_trace",
                lane=response.lane,
                provider=response.provider,
                model=response.model,
                trace_id=completion["trace_id"],
                root_trace_id=completion["root_trace_id"],
                span_id=completion["span_id"],
                parent_span_id=completion["parent_span_id"],
                artifact_id=completion["artifact_id"],
                payload={
                    "app_session_id": session_id,
                    "trace": reasoning_trace[:2000],
                    "trace_length": len(reasoning_trace),
                    "visible_response_length": len(response.content),
                },
            )
        self.observe.emit(
            "brain_turn_complete",
            lane=response.lane,
            provider=response.provider,
            model=response.model,
            trace_id=completion["trace_id"],
            root_trace_id=completion["root_trace_id"],
            span_id=completion["span_id"],
            parent_span_id=completion["parent_span_id"],
            artifact_id=completion["artifact_id"],
            payload={
                "app_session_id": session_id,
                "provider_session_id": response.artifacts.get("session_id"),
                "response_length": len(response.content),
            },
        )
