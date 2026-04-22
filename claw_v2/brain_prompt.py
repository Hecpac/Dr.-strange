from __future__ import annotations

import logging
from dataclasses import dataclass

from claw_v2.adapters.base import UserContentBlock, UserPrompt
from claw_v2.learning import LearningLoop
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.playbook_loader import PlaybookLoader

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PromptBuilder:
    memory: MemoryStore
    observe: ObserveStream | None = None
    learning: LearningLoop | None = None
    wiki: object | None = None
    playbooks: PlaybookLoader | None = None

    def build(
        self,
        *,
        session_id: str,
        message: UserPrompt,
        stored_user_message: str,
        include_history: bool,
        catchup_after_id: int | None,
        task_type: str | None,
    ) -> UserPrompt:
        lessons = self._learning_context(session_id, stored_user_message, task_type)
        lessons = self._with_calibration(lessons, task_type)
        wiki_context = self._wiki_context(stored_user_message)
        if wiki_context:
            lessons = f"{lessons}\n{wiki_context}" if lessons else wiki_context
        if self.playbooks is not None:
            playbook_context = self.playbooks.context_for(stored_user_message)
            if playbook_context:
                lessons = f"{lessons}\n{playbook_context}" if lessons else playbook_context
        autonomy_contract = self._autonomy_contract(session_id, task_type=task_type)
        if autonomy_contract:
            lessons = f"{lessons}\n{autonomy_contract}" if lessons else autonomy_contract
        return self._join_user_prompt(
            session_id=session_id,
            message=message,
            lessons=lessons,
            include_history=include_history,
            catchup_after_id=catchup_after_id,
        )

    def _learning_context(self, session_id: str, message: str, task_type: str | None) -> str:
        if self.learning is None:
            return ""
        lessons, sparsity = self.learning.retrieve_lessons(message, task_type=task_type)
        if lessons and self.observe is not None:
            first_tag_end = lessons.find("</learned_lesson>")
            preview = lessons[:first_tag_end + len("</learned_lesson>")] if first_tag_end >= 0 else lessons[:400]
            self.observe.emit(
                "experience_replay_retrieved",
                payload={
                    "session_id": session_id,
                    "task_type": task_type,
                    "lesson_count": lessons.count("<learned_lesson"),
                    "preview": preview[:400],
                    "max_similarity": sparsity.max_similarity,
                    "graph_expansion_count": sparsity.graph_expansion_count,
                },
            )
        if (sparsity.max_similarity < 0.4) and (sparsity.graph_expansion_count == 0):
            ood_block = (
                "<ood_warning>\n"
                "Sparsity Alert: Your memory search returned low-confidence results. "
                "You are currently in an Out-of-Distribution zone. "
                "No strong analogical patterns were found in your past experiences. "
                "Proceed with extreme caution, verify every assumption, and verbalize your uncertainty to the user.\n"
                "</ood_warning>"
            )
            lessons = f"{ood_block}\n{lessons}" if lessons else ood_block
            if self.observe is not None:
                self.observe.emit(
                    "ood_detected",
                    payload={
                        "session_id": session_id,
                        "max_similarity": sparsity.max_similarity,
                        "graph_expansion_count": sparsity.graph_expansion_count,
                        "total_relevant_lessons": sparsity.total_relevant_lessons,
                    },
                )
        return lessons

    def _with_calibration(self, lessons: str, task_type: str | None) -> str:
        if not task_type:
            return lessons
        cal = self.memory.get_calibration_stats(task_type)
        if not cal or cal["sample_count"] < 5 or abs(cal["calibration_delta"]) <= 0.15:
            return lessons
        delta_pct = abs(round(cal["calibration_delta"] * 100))
        if cal["calibration_delta"] < 0:
            adj = "Be more critical of your certainty"
            direction = "higher"
        else:
            adj = "Trust your intuition more"
            direction = "lower"
        cal_block = (
            "<confidence_calibration>\n"
            f"Based on your historical performance in {task_type} tasks:\n"
            f"- Your predicted confidence is typically {delta_pct}% {direction} "
            f"than your actual success rate.\n"
            f"- Adjustment: {adj}.\n"
            "</confidence_calibration>"
        )
        return f"{cal_block}\n{lessons}" if lessons else cal_block

    def _join_user_prompt(
        self,
        *,
        session_id: str,
        message: UserPrompt,
        lessons: str,
        include_history: bool,
        catchup_after_id: int | None,
    ) -> UserPrompt:
        if isinstance(message, str):
            if not include_history:
                catchup = self._build_catchup(session_id, after_id=catchup_after_id)
                prompt = f"{catchup}{message}" if catchup else message
                return f"{lessons}\n{prompt}" if lessons else prompt
            ctx = self.memory.build_context(session_id, message, include_history=True)
            return f"{lessons}\n{ctx}" if lessons else ctx

        if not include_history:
            catchup = self._build_catchup(session_id, after_id=catchup_after_id)
            preamble = f"{lessons}\n{catchup}" if lessons and catchup else (lessons or catchup)
            if preamble:
                return [{"type": "text", "text": preamble}, *message]
            return message

        context = self.memory.build_context(session_id, include_history=True).strip()
        blocks: list[UserContentBlock] = []
        preamble = f"{lessons}\n{context}" if lessons else context
        marker_text = f"{preamble}\n# Current input" if preamble else "# Current input"
        blocks.append({"type": "text", "text": marker_text})
        blocks.extend(message)
        return blocks

    def _wiki_context(self, message: str) -> str:
        if self.wiki is None:
            return ""
        try:
            results = self.wiki.search(message, limit=3)
        except Exception:
            logger.debug("Wiki search failed", exc_info=True)
            return ""
        if not results:
            return ""
        if _looks_like_knowledge_question(message) and float(results[0].get("similarity", 0.0)) >= 0.35:
            try:
                answer = self.wiki.query(message, archive=False)
            except Exception:
                logger.debug("Wiki query failed", exc_info=True)
                answer = ""
            if answer:
                return f"<wiki-context>\n# Wiki answer\n{answer[:1200]}\n</wiki-context>"
        lines = ["<wiki-context>", "# Wiki context"]
        for result in results:
            lines.append(f"- **{result['title']}** (sim={result['similarity']}): {result['snippet'][:150]}")
        lines.append("</wiki-context>")
        return "\n".join(lines)

    def _build_catchup(self, session_id: str, *, after_id: int | None) -> str:
        if after_id is None:
            return ""
        recent = self.memory.get_messages_since(session_id, after_id, limit=50)
        if not recent:
            return ""
        lines = [f"{row['role']}: {row['content']}" for row in recent]
        return "# Recent context (includes messages outside this session)\n" + "\n".join(lines) + "\n\n"

    def _autonomy_contract(self, session_id: str, *, task_type: str | None) -> str:
        if task_type != "telegram_message":
            return ""
        state = self.memory.get_session_state(session_id)
        autonomy_mode = state.get("autonomy_mode", "assisted")
        mode = state.get("mode", "chat")
        current_goal = state.get("current_goal")
        pending_action = state.get("pending_action")
        if autonomy_mode == "manual":
            return "\n".join(["# Autonomy contract", "Mode: manual", "Ask before taking non-trivial or irreversible actions."])
        lines = ["# Autonomy contract", f"Mode: {autonomy_mode}", f"Workstream: {mode}"]
        if current_goal:
            lines.append(f"Current goal: {current_goal}")
        if pending_action:
            lines.append(f"Pending action: {pending_action}")
        lines.extend(
            [
                "Follow a short task loop internally: inspect context, choose the next safe step, execute or reason through it, then verify what changed.",
                "Do not stop after a plan if the next safe step is obvious.",
                "For coding or technical tasks, prefer end-to-end progress: inspect, edit, verify, summarize.",
                "Truly stuck rule: retry a failing tool at most 3 times, then switch tools; after failures across 3 distinct tools, stop and ask Hector with evidence.",
                "Stop and ask only when blocked, when an action is destructive, or when external publication/authenticated actions need confirmation.",
                "End with a concise operational checkpoint: what was done, what was verified, and what is pending.",
            ]
        )
        if autonomy_mode == "autonomous":
            lines.append("Batch multiple safe intermediate steps before yielding back to the user when that materially advances the task.")
        return "\n".join(lines)


def _looks_like_knowledge_question(message: str) -> bool:
    stripped = message.strip().lower()
    if "?" in stripped:
        return True
    starters = (
        "que ", "qué ", "como ", "cómo ", "cual ", "cuál ", "donde ", "dónde ",
        "when ", "what ", "how ", "why ", "where ", "who ", "explain ", "explica ",
    )
    return any(stripped.startswith(prefix) for prefix in starters)
