"""
Runtime guards para la auditoría del 24-may.

Estos helpers están pensados para integrarse alrededor del dispatcher/brain,
no dentro del agente de redes sociales.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional


IMAGE_ERROR_PATTERNS = [
    "image in the conversation could not be processed",
    "could not be processed and was removed",
    "invalid image",
    "failed to process image",
]


def error_mentions_bad_image(error_text: str) -> bool:
    lowered = (error_text or "").lower()
    return any(pattern in lowered for pattern in IMAGE_ERROR_PATTERNS)


def _strip_image_parts_from_content(content: Any) -> Any:
    """
    Elimina partes de imagen de mensajes estilo OpenAI/Responses:
    - {"type": "input_image", ...}
    - {"type": "image_url", ...}
    - {"type": "image", ...}
    Conserva texto y otros bloques.
    """
    if isinstance(content, list):
        kept = []
        for part in content:
            if isinstance(part, dict):
                part_type = str(part.get("type", "")).lower()
                if part_type in {"input_image", "image_url", "image"}:
                    continue
                if "image_url" in part or "image" in part:
                    continue
            kept.append(part)
        return kept
    return content


def sanitize_llm_messages(
    messages: List[Dict[str, Any]],
    *,
    previous_error: Optional[str] = None,
    drop_all_images: bool = False,
) -> List[Dict[str, Any]]:
    """
    Usa esto antes del siguiente tool/model round.

    Política:
    - Si ya hubo error de imagen rota, no recicles ninguna imagen vieja del contexto.
    - Si drop_all_images=True, fuerza texto-only.
    - Mantiene roles, ids y texto para no perder continuidad.
    """
    should_strip = drop_all_images or error_mentions_bad_image(previous_error or "")
    if not should_strip:
        return messages

    sanitized: List[Dict[str, Any]] = []
    for msg in messages:
        clean = dict(msg)
        if "content" in clean:
            clean["content"] = _strip_image_parts_from_content(clean["content"])
        # Algunos runtimes guardan adjuntos fuera de content.
        for key in ("attachments", "images", "input_images"):
            if key in clean:
                clean[key] = []
        sanitized.append(clean)
    return sanitized


@dataclass
class CircuitBreaker:
    failure_threshold: int = 1
    cooldown_seconds: int = 24 * 60 * 60
    failures: int = 0
    disabled_until: float = 0.0
    last_reason: Optional[str] = None

    def is_open(self) -> bool:
        return time.time() < self.disabled_until

    def record_failure(self, reason: str) -> None:
        self.failures += 1
        self.last_reason = reason
        if self.failures >= self.failure_threshold:
            self.disabled_until = time.time() + self.cooldown_seconds

    def record_success(self) -> None:
        self.failures = 0
        self.disabled_until = 0.0
        self.last_reason = None


class RealtimeTTSGuard:
    """
    Degrada Realtime TTS a batch cuando aparece:
    invalid_request_error.beta_api_shape_disabled
    """
    def __init__(self) -> None:
        self.breaker = CircuitBreaker(failure_threshold=1)

    def should_use_realtime(self) -> bool:
        return not self.breaker.is_open()

    def route(self) -> str:
        return "realtime" if self.should_use_realtime() else "batch"

    def observe_error(self, error_text: str) -> None:
        lowered = (error_text or "").lower()
        if "beta_api_shape_disabled" in lowered or "connectionclosederror 4000" in lowered:
            self.breaker.record_failure("Realtime TTS beta shape disabled; route to batch.")

    def observe_success(self) -> None:
        self.breaker.record_success()


def _port_is_open(host: str, port: int, timeout: float = 0.25) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def wait_for_cdp(port: int = 9250, timeout_seconds: float = 5.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _port_is_open("127.0.0.1", port):
            return True
        time.sleep(0.2)
    return False


def pids_using_port(port: int) -> List[int]:
    """
    macOS/Linux helper. Devuelve [] si lsof no está disponible.
    """
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return []
    return [int(x) for x in result.stdout.split() if x.strip().isdigit()]


def pids_for_profile_dir(profile_dir: str) -> List[int]:
    """
    Busca procesos que mencionan el perfil. En macOS/Linux.
    """
    if not profile_dir:
        return []
    try:
        result = subprocess.run(["ps", "axo", "pid=,command="], check=False, capture_output=True, text=True)
    except FileNotFoundError:
        return []
    pids: List[int] = []
    for line in result.stdout.splitlines():
        if profile_dir in line and "grep" not in line:
            parts = line.strip().split(maxsplit=1)
            if parts and parts[0].isdigit():
                pids.append(int(parts[0]))
    return pids


def kill_pids(pids: Iterable[int], *, grace_seconds: float = 1.0) -> None:
    unique = sorted(set(int(pid) for pid in pids if int(pid) > 1))
    for pid in unique:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(grace_seconds)
    for pid in unique:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def chrome_watchdog_cleanup(profile_dir: str, port: int = 9250) -> Dict[str, Any]:
    """
    Si CDP no levanta en 5s, limpia PIDs huérfanos del puerto/perfil.
    """
    if wait_for_cdp(port=port, timeout_seconds=5.0):
        return {"status": "cdp_ready", "killed_pids": []}

    pids = sorted(set(pids_using_port(port) + pids_for_profile_dir(profile_dir)))
    kill_pids(pids)
    return {"status": "cleaned_orphaned_chrome", "killed_pids": pids}


@dataclass
class Tier3Context:
    user_opened_session: bool = False
    browser_ready: bool = False
    authenticated: bool = False
    action_is_irreversible: bool = False
    user_supplied_copy: bool = False
    target_platform: Optional[str] = None
    ambiguity_count: int = 0


def tier3_requires_confirmation(ctx: Tier3Context) -> bool:
    """
    Evita sobre-escalar en flujos donde el usuario ya abrió la sesión.

    Requiere confirmación cuando:
    - la acción es irreversible/publica y no hay copy suministrado,
    - no hay autenticación/sesión lista,
    - hay ambigüedad real de destino o contenido.
    """
    if not (ctx.user_opened_session or ctx.browser_ready or ctx.authenticated):
        return True
    if ctx.ambiguity_count >= 2:
        return True
    if ctx.action_is_irreversible and not ctx.user_supplied_copy:
        return True
    return False


VOSEO_REPLACEMENTS = {
    r"\btenés\b": "tienes",
    r"\bTenés\b": "Tienes",
    r"\bquerés\b": "quieres",
    r"\bQuerés\b": "Quieres",
    r"\bpodés\b": "puedes",
    r"\bPodés\b": "Puedes",
    r"\bsos\b": "eres",
    r"\bSos\b": "Eres",
    r"\bhacé\b": "haz",
    r"\bHacé\b": "Haz",
    r"\bdecime\b": "dime",
    r"\bDecime\b": "Dime",
}


def neutral_latam_rewrite(text: str) -> str:
    clean = text
    for pattern, replacement in VOSEO_REPLACEMENTS.items():
        clean = re.sub(pattern, replacement, clean)
    return clean


def render_failure_event(event: Dict[str, Any]) -> str:
    """
    Convierte un evento de error a un resumen de baja fricción para logs humanos.
    """
    return json.dumps(
        {
            "kind": event.get("kind", "runtime_failure"),
            "root_cause": event.get("root_cause"),
            "retry_policy": event.get("retry_policy"),
            "next_action": event.get("next_action"),
        },
        ensure_ascii=False,
        indent=2,
    )
