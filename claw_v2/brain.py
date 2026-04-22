from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING

from claw_v2.adapters.base import UserPrompt
from claw_v2.approval import ApprovalManager
from claw_v2.brain_execution import ExecutionEventEmitter, ExecutionGatingService, SnapshotOrchestrator
from claw_v2.brain_json import _first_json_object, _normalize_recommendation, _normalize_risk_level
from claw_v2.brain_json import _strip_trace_tags, _try_parse_json_object, _validate_schema_keys
from claw_v2.brain_lifecycle import VerificationOutcomeRecorder, _count_recent_consecutive_failures
from claw_v2.brain_prompt import PromptBuilder, _looks_like_knowledge_question
from claw_v2.brain_response import BRAIN_RESPONSE_CONTRACT, SELF_HEALING_LOOP_CONTRACT, _brain_system_prompt
from claw_v2.brain_response import _extract_visible_brain_response, _summarize_user_prompt
from claw_v2.brain_structured import StructuredResponseService
from claw_v2.brain_turn import BrainTurnService
from claw_v2.brain_verifier import VERIFIER_PROMPT, VerifierVotingService
from claw_v2.brain_verifier_votes import _aggregate_verifier_votes, _format_verifier_evidence, _parse_verifier_payload
from claw_v2.brain_verifier_votes import _serializable_verifier_votes, _verifier_error_vote
from claw_v2.learning import LearningLoop
from claw_v2.llm import LLMRouter
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.playbook_loader import PlaybookLoader
from claw_v2.types import CriticalActionExecution, CriticalActionVerification, LLMResponse

if TYPE_CHECKING:
    from claw_v2.checkpoint import CheckpointService


@dataclass(slots=True)
class BrainService:
    router: LLMRouter
    memory: MemoryStore
    system_prompt: str
    approvals: ApprovalManager | None = None
    observe: ObserveStream | None = None
    learning: LearningLoop | None = None
    checkpoint: "CheckpointService | None" = None
    wiki: object | None = None
    playbooks: PlaybookLoader | None = None
    _last_confidence: float = 0.0

    def __post_init__(self) -> None:
        if self.playbooks is None:
            self.playbooks = PlaybookLoader()

    def handle_message(
        self,
        session_id: str,
        message: UserPrompt,
        *,
        memory_text: str | None = None,
        task_type: str | None = None,
    ) -> LLMResponse:
        response = BrainTurnService(
            router=self.router,
            memory=self.memory,
            system_prompt=self.system_prompt,
            build_prompt=self._build_prompt,
            observe=self.observe,
        ).handle_message(
            session_id,
            message,
            memory_text=memory_text,
            task_type=task_type,
        )
        self._last_confidence = response.confidence
        return response

    def handle_structured(
        self,
        session_id: str,
        message: str,
        *,
        schema: dict[str, Any],
        task_type: str | None = None,
        store_history: bool = True,
        max_retries: int = 1,
    ) -> dict[str, Any]:
        return StructuredResponseService(
            memory=self.memory,
            handle_message=self.handle_message,
        ).handle(
            session_id,
            message,
            schema=schema,
            task_type=task_type,
            store_history=store_history,
            max_retries=max_retries,
        )

    def verify_critical_action(
        self,
        *,
        plan: str,
        diff: str,
        test_output: str,
        action: str = "critical_action",
        create_approval: bool = True,
    ) -> CriticalActionVerification:
        return self._verifier().verify(
            plan=plan,
            diff=diff,
            test_output=test_output,
            action=action,
            create_approval=create_approval,
        )

    def execute_critical_action(
        self,
        *,
        action: str,
        plan: str,
        diff: str,
        test_output: str,
        executor: Callable[[], Any],
        autonomy_mode: str = "assisted",
        approval_id: str | None = None,
        pre_check: Callable[[CriticalActionVerification], bool] | None = None,
    ) -> CriticalActionExecution:
        return self._execution_service().execute(
            action=action,
            plan=plan,
            diff=diff,
            test_output=test_output,
            executor=executor,
            verify_action=self.verify_critical_action,
            autonomy_mode=autonomy_mode,
            approval_id=approval_id,
            pre_check=pre_check,
        )

    def _build_prompt(
        self,
        *,
        session_id: str,
        message: UserPrompt,
        stored_user_message: str,
        include_history: bool,
        catchup_after_id: int | None,
        task_type: str | None,
    ) -> UserPrompt:
        return self._prompt_builder().build(
            session_id=session_id,
            message=message,
            stored_user_message=stored_user_message,
            include_history=include_history,
            catchup_after_id=catchup_after_id,
            task_type=task_type,
        )

    def _emit_verification_outcome(
        self,
        *,
        session_id: str,
        task_type: str,
        goal: str,
        action_summary: str,
        verification_status: str,
        error_snippet: str | None,
        predicted_confidence: float | None = None,
    ) -> None:
        self._outcomes().record(
            session_id=session_id,
            task_type=task_type,
            goal=goal,
            action_summary=action_summary,
            verification_status=verification_status,
            error_snippet=error_snippet,
            predicted_confidence=predicted_confidence,
        )

    def _collect_verifier_vote(self, *, evidence: dict, provider: str, model: str, role: str) -> dict:
        return self._verifier().collect_vote(evidence=evidence, provider=provider, model=model, role=role)

    def _secondary_verifier_provider(self, primary_provider: str) -> str | None:
        return self._verifier().secondary_provider(primary_provider)

    def _maybe_pre_snapshot(self, *, action: str, session_id: str | None = None) -> str | None:
        return self._snapshots().pre_snapshot(action=action, session_id=session_id)

    def _emit_execution_event(
        self,
        *,
        action: str,
        verification: CriticalActionVerification,
        status: str,
        approval_status: str | None,
        checkpoint_id: str | None = None,
    ) -> None:
        self._events().emit(
            action=action,
            verification=verification,
            status=status,
            approval_status=approval_status,
            checkpoint_id=checkpoint_id,
        )

    def _wiki_context(self, message: str) -> str:
        return self._prompt_builder()._wiki_context(message)

    def _build_catchup(self, session_id: str, *, after_id: int | None) -> str:
        return self._prompt_builder()._build_catchup(session_id, after_id=after_id)

    def _autonomy_contract(self, session_id: str, *, task_type: str | None) -> str:
        return self._prompt_builder()._autonomy_contract(session_id, task_type=task_type)

    def _prompt_builder(self) -> PromptBuilder:
        return PromptBuilder(
            memory=self.memory,
            observe=self.observe,
            learning=self.learning,
            wiki=self.wiki,
            playbooks=self.playbooks,
        )

    def _verifier(self) -> VerifierVotingService:
        return VerifierVotingService(router=self.router, approvals=self.approvals, observe=self.observe)

    def _outcomes(self) -> VerificationOutcomeRecorder:
        return VerificationOutcomeRecorder(
            memory=self.memory,
            observe=self.observe,
            learning=self.learning,
            checkpoint=self.checkpoint,
        )

    def _snapshots(self) -> SnapshotOrchestrator:
        return SnapshotOrchestrator(checkpoint=self.checkpoint)

    def _events(self) -> ExecutionEventEmitter:
        return ExecutionEventEmitter(
            observe=self.observe,
            outcomes=self._outcomes(),
            predicted_confidence=self._last_confidence or None,
        )

    def _execution_service(self) -> ExecutionGatingService:
        return ExecutionGatingService(
            approvals=self.approvals,
            snapshots=self._snapshots(),
            events=self._events(),
        )
