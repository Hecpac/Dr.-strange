"""Telegram/CLI handler for checkpoint management.

Provides /rollback <ckpt_id|last> and /checkpoints list.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from claw_v2.bot_commands import BotCommand, CommandContext

if TYPE_CHECKING:
    from claw_v2.checkpoint import CheckpointService

logger = logging.getLogger(__name__)


class CheckpointHandler:
    def __init__(self, *, checkpoint: "CheckpointService") -> None:
        self.checkpoint = checkpoint

    def commands(self) -> list[BotCommand]:
        return [
            BotCommand(
                "rollback",
                self.handle_command,
                exact=("/rollback",),
                prefixes=("/rollback ",),
            ),
            BotCommand(
                "checkpoints",
                self.handle_command,
                exact=("/checkpoints",),
                prefixes=("/checkpoints ",),
            ),
        ]

    def handle_command(self, context: CommandContext) -> str:
        stripped = context.stripped.strip()
        if stripped == "/rollback" or stripped.startswith("/rollback "):
            return self._handle_rollback(stripped)
        if stripped == "/checkpoints" or stripped.startswith("/checkpoints "):
            return self._handle_checkpoints(stripped)
        return "Comando no reconocido."

    def _handle_rollback(self, stripped: str) -> str:
        parts = stripped.split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else ""
        if not target:
            return (
                "Uso: /rollback <ckpt_id|last>\n"
                "Usa /checkpoints list para ver IDs disponibles."
            )
        if target == "last":
            row = self.checkpoint.latest()
            if row is None:
                return "No hay checkpoints disponibles. Crea uno primero."
            ckpt_id = row["ckpt_id"]
        else:
            ckpt_id = target
        try:
            self.checkpoint.schedule_restore(ckpt_id)
        except KeyError:
            return f"Checkpoint {ckpt_id} no encontrado."
        except FileNotFoundError:
            return f"Checkpoint {ckpt_id} tiene su archivo snapshot ausente del disco."
        return (
            f"Checkpoint {ckpt_id} marcado para rollback. "
            "Ejecuta /restart para aplicar."
        )

    def _handle_checkpoints(self, stripped: str) -> str:
        parts = stripped.split(maxsplit=1)
        sub = parts[1].strip() if len(parts) > 1 else ""
        if sub != "list":
            return (
                "Uso: /checkpoints list — muestra los checkpoints disponibles."
            )
        rows = self.checkpoint.list()
        if not rows:
            return "Sin checkpoints registrados."
        lines = ["Checkpoints disponibles (más reciente primero):"]
        for r in rows:
            lines.append(
                f"· {r['ckpt_id']} — {r['created_at']} — {r['trigger_reason'][:60]}"
            )
        return "\n".join(lines)
