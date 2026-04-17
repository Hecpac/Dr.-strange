from __future__ import annotations

import json
import logging
from typing import Any, Callable

from claw_v2.bot_commands import BotCommand, CommandContext

logger = logging.getLogger(__name__)

_DESIGN_URL = "https://claude.ai/design"

_DESIGN_BRAIN_PROMPT = """\
El usuario quiere crear un prototipo en Claude Design.
Brief del usuario: {brief}

Instrucciones para ejecutar:
1. Navega a {design_url} via Chrome CDP (puerto del managed_chrome).
2. Crea un nuevo prototipo (Wireframe) con el nombre derivado del brief.
3. Escribe el brief del usuario en el campo de diseño y envíalo.
4. Espera a que Claude Design genere el wireframe (~30-60s). Toma screenshots para monitorear.
5. Cuando termine, captura un screenshot del resultado y envíalo por Telegram via send_photo.
6. Reporta al usuario qué se generó, cuántas variaciones, y el link al proyecto.

Usa chrome_navigate para navegar, chrome_screenshot para capturar, y send_photo para enviar la imagen.
El Chrome autenticado ya tiene sesión activa en claude.ai.
"""


class DesignHandler:
    def __init__(
        self,
        browser: Any | None = None,
        capability_check: Callable[[str, str], str | None] | None = None,
        get_managed_chrome: Callable[[], Any] | None = None,
    ) -> None:
        self.browser = browser
        self._check_capability = capability_check or (lambda name, fallback: None)
        self._get_managed_chrome = get_managed_chrome or (lambda: None)

    def commands(self) -> list[BotCommand]:
        return [
            BotCommand(
                "design",
                self.handle_command,
                exact=("/design",),
                prefixes=("/design ",),
            ),
        ]

    def handle_command(self, context: CommandContext) -> str:
        stripped = context.stripped
        if stripped == "/design":
            return (
                "Uso: /design <brief>\n\n"
                "Ejemplo: /design Landing page para estudio de diseño AI, "
                "tema oscuro, hero con tipografía grande, grid de servicios"
            )
        brief = stripped.split(maxsplit=1)[1]
        degraded = self._check_capability("chrome_cdp", "Chrome no disponible.")
        if degraded is not None:
            return degraded
        managed_chrome = self._get_managed_chrome()
        if self.browser is None or managed_chrome is None:
            return "Chrome no disponible. Necesito Chrome CDP para usar Claude Design."

        from claw_v2.state_handler import _BrainShortcut

        return _BrainShortcut(
            text=_DESIGN_BRAIN_PROMPT.format(brief=brief, design_url=_DESIGN_URL),
            memory_text=f"/design {brief}",
        )
