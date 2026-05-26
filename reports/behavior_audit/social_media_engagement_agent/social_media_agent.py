#!/usr/bin/env python3
"""
Social Media Engagement Agent — MVP sin dependencias externas.

Objetivo:
- Crear posts, variantes A/B, CTAs, prompts de comentarios, calendario y plan de medición.
- Servir como "Pattern B + MVP": un agente coordinador con skills determinísticas
  que se puede conectar a browser/CDP, research externo o publicadores cuando estén disponibles.

No publica por sí solo. Devuelve un brief listo para revisión/publicación.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Callable, Dict, List, Optional, Sequence, Any


class Platform(str, Enum):
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    LINKEDIN = "linkedin"
    X = "x"
    THREADS = "threads"
    YOUTUBE_SHORTS = "youtube_shorts"


@dataclass
class BrandProfile:
    name: str = "Marca"
    voice: str = "clara, directa, útil, cercana, español neutral latam"
    forbidden: List[str] = field(default_factory=lambda: ["voseo", "promesas no verificables", "clickbait engañoso"])
    positioning: str = "ayuda a su audiencia a tomar mejores decisiones con contenido práctico"
    proof_points: List[str] = field(default_factory=list)


@dataclass
class ContentRequest:
    platform: Platform
    topic: str
    goal: str
    audience: str
    format_hint: str = "post"
    brand: BrandProfile = field(default_factory=BrandProfile)
    constraints: List[str] = field(default_factory=list)


@dataclass
class PostCandidate:
    platform: str
    format: str
    hook: str
    caption: str
    cta: str
    hashtags: List[str]
    engagement_mechanic: str
    alt_text: str
    risk_notes: List[str] = field(default_factory=list)
    expected_metric: str = "engagement rate"


@dataclass
class EngagementPlan:
    objective: str
    primary_metric: str
    secondary_metrics: List[str]
    comment_prompts: List[str]
    reply_templates: List[str]
    ab_tests: List[Dict[str, str]]
    publishing_notes: List[str]


@dataclass
class CampaignOutput:
    request: ContentRequest
    posts: List[PostCandidate]
    engagement_plan: EngagementPlan
    calendar: List[Dict[str, str]]
    safety_check: Dict[str, Any]


def _slug_words(text: str, limit: int = 8) -> List[str]:
    words = re.findall(r"[a-zA-ZáéíóúÁÉÍÓÚñÑ0-9]+", text.lower())
    stop = {"de", "la", "el", "los", "las", "un", "una", "para", "con", "sin", "por", "y", "o", "en", "del"}
    return [w for w in words if w not in stop][:limit]


def _neutral_latam(text: str) -> str:
    """Pequeña normalización para evitar voseo común."""
    replacements = {
        "tenés": "tienes",
        "Tenés": "Tienes",
        "querés": "quieres",
        "Querés": "Quieres",
        "podés": "puedes",
        "Podés": "Puedes",
        "sos ": "eres ",
        "Sos ": "Eres ",
        "hacé": "haz",
        "Hacé": "Haz",
        "decime": "dime",
        "Decime": "Dime",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text


class Skill:
    name: str

    def run(self, request: ContentRequest, state: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class HookLabSkill(Skill):
    name = "hook_lab"

    def run(self, request: ContentRequest, state: Dict[str, Any]) -> Dict[str, Any]:
        topic = request.topic
        audience = request.audience
        hooks = [
            f"Lo que nadie te dice sobre {topic}",
            f"3 señales de que estás abordando mal {topic}",
            f"Guarda esto si quieres mejorar en {topic}",
            f"Antes de decidir sobre {topic}, revisa esto",
            f"El error más común de {audience} con {topic}",
            f"Una forma simple de convertir {topic} en resultados",
            f"Esto cambió mi manera de pensar sobre {topic}",
            f"Checklist rápido para {topic}",
            f"Si estás empezando con {topic}, evita esto",
            f"Cómo explicar {topic} sin complicarlo",
        ]
        return {"hooks": [_neutral_latam(h) for h in hooks]}


class CaptionWriterSkill(Skill):
    name = "caption_writer"

    def run(self, request: ContentRequest, state: Dict[str, Any]) -> Dict[str, Any]:
        hooks: Sequence[str] = state.get("hooks", [])
        platform = request.platform.value
        topic = request.topic
        audience = request.audience
        goal = request.goal

        platform_guidance = {
            Platform.INSTAGRAM.value: "Usa líneas cortas, guardables y CTA claro a comentar/guardar.",
            Platform.TIKTOK.value: "Escribe como guion hablado: rápido, visual y con remate.",
            Platform.LINKEDIN.value: "Usa tesis, ejemplo, aprendizaje y pregunta final.",
            Platform.X.value: "Sé compacto. Una idea fuerte por post o hilo breve.",
            Platform.THREADS.value: "Tono conversacional, directo y con pregunta abierta.",
            Platform.YOUTUBE_SHORTS.value: "Guion de 35–55 segundos con gancho en los primeros 2 segundos.",
        }.get(platform, "Sé claro y útil.")

        captions = []
        for i, hook in enumerate(hooks[:4], start=1):
            if request.platform == Platform.LINKEDIN:
                caption = (
                    f"{hook}\n\n"
                    f"Para {audience}, el punto no es publicar más sobre {topic}; "
                    f"es publicar con una hipótesis clara.\n\n"
                    f"Prueba este marco:\n"
                    f"1. Define qué decisión ayudas a tomar.\n"
                    f"2. Muestra un ejemplo concreto.\n"
                    f"3. Cierra con una pregunta que invite experiencia real.\n\n"
                    f"Objetivo del post: {goal}.\n\n"
                    f"{platform_guidance}"
                )
            elif request.platform in {Platform.TIKTOK, Platform.YOUTUBE_SHORTS}:
                caption = (
                    f"{hook}\n\n"
                    f"Guion:\n"
                    f"0–2s: plantea el problema de {topic}.\n"
                    f"3–15s: muestra el error frecuente.\n"
                    f"16–35s: da una táctica aplicable.\n"
                    f"Final: pide que comenten su caso para responder con una recomendación.\n\n"
                    f"{platform_guidance}"
                )
            else:
                caption = (
                    f"{hook}\n\n"
                    f"Si trabajas en {topic}, no necesitas más ruido: necesitas una acción clara.\n\n"
                    f"Prueba esto hoy:\n"
                    f"• Nombra el problema en una frase.\n"
                    f"• Da un ejemplo real o cercano.\n"
                    f"• Cierra con una pregunta específica.\n\n"
                    f"Meta: {goal}.\n\n"
                    f"{platform_guidance}"
                )
            captions.append(_neutral_latam(caption))
        return {"captions": captions}


class EngagementMechanicsSkill(Skill):
    name = "engagement_mechanics"

    def run(self, request: ContentRequest, state: Dict[str, Any]) -> Dict[str, Any]:
        mechanics = {
            Platform.INSTAGRAM: [
                "CTA de guardado: 'Guarda esto para revisar antes de publicar'.",
                "Pregunta de comentario: '¿Cuál de estos puntos te cuesta más: 1, 2 o 3?'",
                "Sticker/encuesta en stories después del post para reactivar alcance.",
            ],
            Platform.TIKTOK: [
                "Pregunta para comentarios: 'Comenta tu caso y respondo con una idea de guion'.",
                "Reply-to-comment video para convertir comentarios en nuevos contenidos.",
                "Gancho de seguimiento: 'Parte 2 con ejemplos reales'.",
            ],
            Platform.LINKEDIN: [
                "Pregunta abierta al final para invitar experiencias, no opiniones genéricas.",
                "Comentario fijado con recurso/checklist.",
                "Responder en la primera hora con preguntas de profundización.",
            ],
            Platform.X: [
                "Cierre con pregunta binaria para facilitar respuesta.",
                "Hilo corto con promesa de checklist.",
                "Citar respuestas útiles para amplificar conversación.",
            ],
            Platform.THREADS: [
                "Pregunta informal de baja fricción.",
                "Respuesta rápida a comentarios con ejemplos.",
                "Post de seguimiento con aprendizajes del hilo.",
            ],
            Platform.YOUTUBE_SHORTS: [
                "CTA hablado y en texto: 'comenta tu industria'.",
                "Fijar comentario con pregunta específica.",
                "Usar comentarios como fuente del siguiente short.",
            ],
        }.get(request.platform, ["Pregunta final específica y respuesta activa durante la primera hora."])

        ctas = [
            "Comenta 'checklist' y dime tu caso.",
            "Guarda esto para aplicarlo antes de tu próxima publicación.",
            "Comparte qué parte te gustaría que convierta en ejemplo.",
            "Responde con 1, 2 o 3 según lo que más te cuesta.",
        ]
        return {"mechanics": mechanics, "ctas": ctas}


class HashtagKeywordSkill(Skill):
    name = "hashtag_keyword_pack"

    def run(self, request: ContentRequest, state: Dict[str, Any]) -> Dict[str, Any]:
        tokens = _slug_words(" ".join([request.topic, request.audience, request.goal]), limit=10)
        hashtags = []
        for token in tokens:
            cleaned = re.sub(r"[^a-zA-Záéíóúñ0-9]", "", token)
            if cleaned:
                hashtags.append("#" + cleaned)
        generic = {
            Platform.INSTAGRAM: ["#contenido", "#marketingdigital", "#creadores"],
            Platform.TIKTOK: ["#tips", "#aprendeentiktok", "#creadores"],
            Platform.LINKEDIN: ["#estrategia", "#negocios", "#productividad"],
            Platform.X: ["#estrategia", "#creadores"],
            Platform.THREADS: ["#ideas", "#creadores"],
            Platform.YOUTUBE_SHORTS: ["#shorts", "#tips", "#creadores"],
        }.get(request.platform, ["#estrategia"])
        # Mantenerlo sobrio: demasiados hashtags reducen señal editorial.
        return {"hashtags": list(dict.fromkeys(hashtags + generic))[:8]}


class CommentReplyPackSkill(Skill):
    name = "comment_reply_pack"

    def run(self, request: ContentRequest, state: Dict[str, Any]) -> Dict[str, Any]:
        replies = [
            "Buen punto. ¿En qué contexto lo estás aplicando ahora?",
            "Gracias por compartirlo. Con ese caso, empezaría por simplificar el mensaje principal.",
            "Totalmente. La clave es convertir esa idea en una acción visible para la audiencia.",
            "Interesante. ¿Quieres que lo convierta en ejemplo paso a paso?",
            "Ese caso da para una parte 2. Lo tomaría como punto de partida.",
        ]
        prompts = [
            "¿Cuál es el mayor bloqueo que tienes con este tema?",
            "¿Qué ejemplo real te gustaría ver?",
            "¿Lo usarías para vender, educar o construir comunidad?",
            "¿Qué parte te gustaría que convierta en checklist?",
        ]
        return {"reply_templates": [_neutral_latam(r) for r in replies], "comment_prompts": prompts}


class ABTestSkill(Skill):
    name = "ab_test_pack"

    def run(self, request: ContentRequest, state: Dict[str, Any]) -> Dict[str, Any]:
        hooks = state.get("hooks", [])
        ab_tests = [
            {
                "variable": "hook",
                "A": hooks[0] if len(hooks) > 0 else f"Guarda esto sobre {request.topic}",
                "B": hooks[1] if len(hooks) > 1 else f"El error más común con {request.topic}",
                "metric": "retención inicial / comentarios",
            },
            {
                "variable": "CTA",
                "A": "Comenta tu caso.",
                "B": "Guarda esto y responde con 1, 2 o 3.",
                "metric": "comentarios / guardados",
            },
            {
                "variable": "formato",
                "A": "checklist",
                "B": "caso antes/después",
                "metric": "guardados / compartidos",
            },
        ]
        return {"ab_tests": ab_tests}


class CalendarSkill(Skill):
    name = "weekly_calendar"

    def run(self, request: ContentRequest, state: Dict[str, Any]) -> Dict[str, Any]:
        topic = request.topic
        return {
            "calendar": [
                {"day": "Día 1", "format": "post principal", "angle": f"checklist sobre {topic}"},
                {"day": "Día 2", "format": "story / hilo corto", "angle": "pregunta a la audiencia + encuesta"},
                {"day": "Día 3", "format": "reply content", "angle": "responder un comentario con ejemplo"},
                {"day": "Día 5", "format": "caso práctico", "angle": "antes/después o mini auditoría"},
                {"day": "Día 7", "format": "recap", "angle": "aprendizajes + CTA a guardar/compartir"},
            ]
        }


class SafetySkill(Skill):
    name = "brand_safety"

    def run(self, request: ContentRequest, state: Dict[str, Any]) -> Dict[str, Any]:
        joined = " ".join(state.get("captions", []))
        risk_notes = []
        for forbidden in request.brand.forbidden:
            if forbidden.lower() in joined.lower():
                risk_notes.append(f"Detectado término o patrón prohibido: {forbidden}")
        if "garantiza" in joined.lower() or "100%" in joined:
            risk_notes.append("Evitar promesas absolutas o no verificables.")
        return {
            "safety_check": {
                "status": "needs_review" if risk_notes else "pass",
                "risk_notes": risk_notes,
                "style": "español neutral latam, sin voseo",
            }
        }


class SocialMediaEngagementAgent:
    def __init__(self, skills: Optional[List[Skill]] = None) -> None:
        self.skills = skills or [
            HookLabSkill(),
            CaptionWriterSkill(),
            EngagementMechanicsSkill(),
            HashtagKeywordSkill(),
            CommentReplyPackSkill(),
            ABTestSkill(),
            CalendarSkill(),
            SafetySkill(),
        ]

    def run_skills(self, request: ContentRequest) -> Dict[str, Any]:
        state: Dict[str, Any] = {}
        for skill in self.skills:
            state.update(skill.run(request, state))
        return state

    def create_campaign(self, request: ContentRequest) -> CampaignOutput:
        state = self.run_skills(request)
        captions = state.get("captions", [])
        hooks = state.get("hooks", [])
        ctas = state.get("ctas", [])
        mechanics = state.get("mechanics", [])
        hashtags = state.get("hashtags", [])

        posts: List[PostCandidate] = []
        for idx, caption in enumerate(captions[:3]):
            hook = hooks[idx] if idx < len(hooks) else hooks[0] if hooks else request.topic
            cta = ctas[idx % len(ctas)] if ctas else "Comenta tu caso."
            mechanic = mechanics[idx % len(mechanics)] if mechanics else "Responder comentarios en la primera hora."
            posts.append(
                PostCandidate(
                    platform=request.platform.value,
                    format=request.format_hint,
                    hook=_neutral_latam(hook),
                    caption=_neutral_latam(caption + "\n\n" + cta),
                    cta=_neutral_latam(cta),
                    hashtags=hashtags,
                    engagement_mechanic=_neutral_latam(mechanic),
                    alt_text=f"Contenido sobre {request.topic} para {request.audience}.",
                    risk_notes=state.get("safety_check", {}).get("risk_notes", []),
                    expected_metric="comentarios, guardados y compartidos",
                )
            )

        engagement_plan = EngagementPlan(
            objective=request.goal,
            primary_metric="comentarios cualificados" if "coment" in request.goal.lower() else "engagement rate",
            secondary_metrics=["guardados", "compartidos", "retención inicial", "respuestas en primera hora"],
            comment_prompts=state.get("comment_prompts", []),
            reply_templates=state.get("reply_templates", []),
            ab_tests=state.get("ab_tests", []),
            publishing_notes=[
                "Publicar cuando puedas responder durante los primeros 30–60 minutos.",
                "Fijar un comentario con pregunta específica o recurso prometido.",
                "Convertir los mejores comentarios en una pieza de seguimiento.",
                "No usar automatización agresiva de likes/follows; priorizar respuestas humanas.",
            ],
        )

        return CampaignOutput(
            request=request,
            posts=posts,
            engagement_plan=engagement_plan,
            calendar=state.get("calendar", []),
            safety_check=state.get("safety_check", {"status": "unknown"}),
        )


def _parse_platform(value: str) -> Platform:
    normalized = value.strip().lower()
    aliases = {
        "ig": "instagram",
        "insta": "instagram",
        "twitter": "x",
        "linkedin": "linkedin",
        "tiktok": "tiktok",
        "threads": "threads",
        "shorts": "youtube_shorts",
        "youtube": "youtube_shorts",
    }
    normalized = aliases.get(normalized, normalized)
    try:
        return Platform(normalized)
    except ValueError as exc:
        allowed = ", ".join(p.value for p in Platform)
        raise argparse.ArgumentTypeError(f"Plataforma inválida: {value}. Usa una de: {allowed}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Social Media Engagement Agent — MVP")
    parser.add_argument("--platform", type=_parse_platform, required=True)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--format", default="post", dest="format_hint")
    parser.add_argument("--brand", default="Marca")
    args = parser.parse_args()

    request = ContentRequest(
        platform=args.platform,
        topic=args.topic,
        goal=args.goal,
        audience=args.audience,
        format_hint=args.format_hint,
        brand=BrandProfile(name=args.brand),
    )
    output = SocialMediaEngagementAgent().create_campaign(request)
    print(json.dumps(asdict(output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
