from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claw_v2.bot_commands import BotCommand, CommandContext


@dataclass(slots=True)
class SkillCommandPlugin:
    bot: Any

    def __getattr__(self, name: str) -> Any:
        return getattr(self.bot, name)

    def commands(self) -> list[BotCommand]:
        return [
            BotCommand("buddy", self._handle_buddy_command, exact=("/buddy", "/buddy card", "/buddy hatch", "/buddy stats"), prefixes=("/buddy rename ",)),
            BotCommand("playbooks", self._handle_playbook_command, exact=("/playbooks",), prefixes=("/playbook ",)),
            BotCommand("backtest", self._handle_backtest_command, exact=("/backtest",), prefixes=("/backtest ",)),
            BotCommand("grill", self._handle_grill_command, exact=("/grill",), prefixes=("/grill ",)),
            BotCommand("tdd", self._handle_tdd_command, exact=("/tdd",), prefixes=("/tdd ",)),
            BotCommand("improve_arch", self._handle_improve_arch_command, exact=("/improve_arch",), prefixes=("/improve_arch ",)),
            BotCommand("effort", self._handle_effort_command, exact=("/effort",), prefixes=("/effort ",)),
            BotCommand("verify", self._handle_verify_command, exact=("/verify",), prefixes=("/verify ",)),
            BotCommand("focus", self._handle_focus_command, exact=("/focus",)),
            BotCommand("voice", self._handle_voice_command, exact=("/voice",), prefixes=("/voice ",)),
        ]

    def _handle_buddy_command(self, context: CommandContext) -> str:
        stripped = context.stripped
        if stripped == "/buddy hatch":
            return self._buddy_hatch_response(context.user_id)
        if stripped == "/buddy stats":
            return self._buddy_stats_response(context.user_id)
        if stripped.startswith("/buddy rename "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /buddy rename <name>"
            return self._buddy_rename_response(context.user_id, parts[2])
        return self._buddy_card_response(context.user_id)

    def _handle_playbook_command(self, context: CommandContext) -> str:
        playbooks = self.brain.playbooks
        if not playbooks._loaded:
            playbooks.load()
        if context.stripped == "/playbooks":
            if not playbooks.playbooks:
                return "No hay playbooks disponibles."
            lines = [f"- **{pb.name}** (triggers: {', '.join(pb.triggers[:4])})" for pb in playbooks.playbooks]
            return "Playbooks disponibles:\n" + "\n".join(lines)
        parts = context.stripped.split(maxsplit=1)
        if len(parts) != 2:
            return "usage: /playbook <name>"
        name = parts[1].strip().lower()
        for pb in playbooks.playbooks:
            if pb.name.lower() == name or name in pb.name.lower():
                return f"## {pb.name}\n{pb.content}"
        return f"Playbook no encontrado: {name}\nUsa /playbooks para ver disponibles."

    def _handle_backtest_command(self, context: CommandContext) -> str:
        playbooks = self.brain.playbooks
        if not playbooks._loaded:
            playbooks.load()
        pb_context = ""
        for pb in playbooks.playbooks:
            if "backtest" in pb.name.lower() or "qts" in pb.name.lower():
                pb_context = pb.content
                break
        if context.stripped == "/backtest":
            if pb_context:
                return f"QTS Backtesting listo.\n\nUso: /backtest <instrucción>\nEjemplo: /backtest corre ICT strategy para BTC 1h últimos 30 días\n\n{pb_context[:500]}"
            return "usage: /backtest <instrucción>"
        parts = context.stripped.split(maxsplit=1)
        instruction = parts[1]
        prompt = f"{instruction}\n\n<playbook-context>\n{pb_context}\n</playbook-context>" if pb_context else instruction
        return self._brain_text_response(context.session_id, prompt)

    def _load_skill_content(self, skill_name: str) -> str:
        skill_path = Path(__file__).parent.parent / "skills" / skill_name / "skill.md"
        if not skill_path.is_file():
            return ""
        text = skill_path.read_text(encoding="utf-8")
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                text = text[end + 3:].strip()
        return text

    def _handle_grill_command(self, context: CommandContext) -> str:
        if context.stripped == "/grill":
            return "Uso: /grill <descripción del plan o diseño>\nEjemplo: /grill migrar auth a OAuth2 con refresh tokens"
        parts = context.stripped.split(maxsplit=1)
        skill_content = self._load_skill_content("grill-me")
        prompt = f"<skill-context>\n{skill_content}\n</skill-context>\n\n{parts[1]}" if skill_content else parts[1]
        return self._brain_text_response(context.session_id, prompt)

    def _handle_tdd_command(self, context: CommandContext) -> str:
        if context.stripped == "/tdd":
            return "Uso: /tdd <feature o bug a implementar>\nEjemplo: /tdd agregar validación de email en registro"
        parts = context.stripped.split(maxsplit=1)
        skill_content = self._load_skill_content("tdd")
        prompt = f"<skill-context>\n{skill_content}\n</skill-context>\n\n{parts[1]}" if skill_content else parts[1]
        return self._brain_text_response(context.session_id, prompt)

    def _handle_improve_arch_command(self, context: CommandContext) -> str:
        skill_content = self._load_skill_content("improve-codebase-architecture")
        if context.stripped == "/improve_arch":
            instruction = "Analiza la arquitectura del codebase actual y sugiere mejoras."
        else:
            parts = context.stripped.split(maxsplit=1)
            instruction = parts[1]
        prompt = f"<skill-context>\n{skill_content}\n</skill-context>\n\n{instruction}" if skill_content else instruction
        return self._brain_text_response(context.session_id, prompt)

    _VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")

    def _handle_effort_command(self, context: CommandContext) -> str:
        if self.config is None:
            return "config not available"
        if context.stripped == "/effort":
            return (
                f"Effort actual:\n"
                f"  brain: {self.config.brain_effort}\n"
                f"  worker: {self.config.worker_effort}\n"
                f"  judge: {self.config.judge_effort}\n"
                f"\nUso: /effort <level> [lane]\n"
                f"Niveles: {', '.join(self._VALID_EFFORTS)}\n"
                f"Lanes: brain, worker, judge (omitir = todas)"
            )
        parts = context.stripped.split()
        level = parts[1].lower() if len(parts) >= 2 else ""
        if level not in self._VALID_EFFORTS:
            return f"Nivel inválido: {level}\nVálidos: {', '.join(self._VALID_EFFORTS)}"
        lane = parts[2].lower() if len(parts) >= 3 else None
        if lane and lane not in ("brain", "worker", "judge"):
            return f"Lane inválido: {lane}\nVálidos: brain, worker, judge"
        if lane:
            setattr(self.config, f"{lane}_effort", level)
        else:
            self.config.brain_effort = level
            self.config.worker_effort = level
            self.config.judge_effort = level
        applied = lane or "todas las lanes"
        return f"Effort → **{level}** para {applied}"

    def _handle_verify_command(self, context: CommandContext) -> str:
        playbooks = self.brain.playbooks
        if not playbooks._loaded:
            playbooks.load()
        pb_context = ""
        for pb in playbooks.playbooks:
            if "verification" in pb.name.lower():
                pb_context = pb.content
                break
        if context.stripped == "/verify":
            instruction = (
                "Ejecuta el Verification Pipeline completo sobre el trabajo actual:\n"
                "Phase 1: Tests — corre pytest, reporta resultados\n"
                "Phase 2: Simplify — revisa git diff, busca mejoras de calidad\n"
                "Phase 3: PR — resume y pregunta si crear PR"
            )
        else:
            parts = context.stripped.split(maxsplit=1)
            instruction = f"Ejecuta verification pipeline sobre: {parts[1]}"
        prompt = f"<playbook-context>\n{pb_context}\n</playbook-context>\n\n{instruction}" if pb_context else instruction
        return self._brain_text_response(context.session_id, prompt)

    def _handle_focus_command(self, context: CommandContext) -> str:
        if not hasattr(self.bot, "_focus_sessions"):
            self.bot._focus_sessions = set()
        sid = context.session_id
        if sid in self.bot._focus_sessions:
            self.bot._focus_sessions.discard(sid)
            return "Focus mode **desactivado**. Verás trabajo intermedio."
        self.bot._focus_sessions.add(sid)
        return "Focus mode **activado**. Solo verás resultados finales."

    _VALID_VOICES = ("alloy", "echo", "fable", "onyx", "nova", "shimmer")

    def _handle_voice_command(self, context: CommandContext) -> str:
        if not hasattr(self.bot, "_voice_sessions"):
            self.bot._voice_sessions = {}
        sid = context.session_id
        if context.stripped == "/voice":
            if sid in self.bot._voice_sessions:
                voice = self.bot._voice_sessions.pop(sid)
                return f"Voice mode **desactivado** (era: {voice})."
            self.bot._voice_sessions[sid] = "nova"
            return "Voice mode **activado** (voz: nova). Responderé por audio.\nUsa `/voice <voz>` para cambiar: alloy, echo, fable, onyx, nova, shimmer"
        parts = context.stripped.split(maxsplit=1)
        voice = parts[1].lower().strip()
        if voice == "off":
            self.bot._voice_sessions.pop(sid, None)
            return "Voice mode **desactivado**."
        if voice not in self._VALID_VOICES:
            return f"Voz inválida: {voice}\nVálidas: {', '.join(self._VALID_VOICES)}"
        self.bot._voice_sessions[sid] = voice
        return f"Voice mode **activado** (voz: {voice})."
