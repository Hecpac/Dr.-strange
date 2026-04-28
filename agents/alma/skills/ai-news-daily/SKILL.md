---
name: ai-news-daily
description: >
  Brief diario de noticias y tendencias de IA en español, con fuentes verificables.
  Usa este skill cada mañana para entregar a Hector un resumen ejecutivo de lo más
  relevante en IA: papers, lanzamientos de modelos, hilos trend en X, MCP/agents,
  y movimientos de Anthropic/OpenAI/Google/xAI. También aplica cuando Hector
  pregunta "noticias de AI hoy", "qué pasó en AI", "titulares trend", "AI news".
---

# AI News Daily Brief

Entrega un brief diario en español de lo más relevante en IA, con fuentes con fecha y link.

## Inputs requeridos

| Input | Fuente | Requerido |
|-------|--------|-----------|
| Fecha objetivo | Hoy (America/Chicago) | ✅ Sí |
| Cuentas X seed | @sama, @AnthropicAI, @demishassabis, @ylecun, @karpathy, @scaling01, @arceyul | Recomendado |
| Verticales prioritarios | modelos frontier, agents/MCP, papers, regulación, productos | Recomendado |

## Proceso

### 1. Recolección de señales (paralela)
- **WebSearch**: "AI news $today", "Anthropic Claude release", "OpenAI announcement", "AI paper today"
- **Firecrawl**: scrape de cuentas seed en X (timeline + replies con engagement >100)
- **Wiki Claw**: `wiki_search` para detectar follow-ups de hilos previos
- **arXiv RSS**: papers nuevos en cs.AI / cs.CL (últimas 24h)

### 2. Filtro y ranking
Cada item debe tener: fuente verificable + fecha + link. Descarta rumores sin link primario.

Rankea por:
- **Impacto frontier** (nuevo modelo, benchmark roto, capacidad nueva): peso 3
- **Impacto producto** (lanzamiento, plugin, marketplace): peso 2
- **Conversación trend** (hilo viral con >5K engagement): peso 2
- **Paper/research** (citaciones tempranas o tema relevante): peso 1

Top 5 items.

### 3. Síntesis en español

Formato exacto del output:

```
🌅 AI Brief — {fecha} CDT

1. [Tema headline]
   Qué pasó: 1-2 frases.
   Por qué importa: 1 frase con ángulo accionable para Hector.
   Fuente: [handle/medio] — link

2. ...

📊 Trend del día
[1 frase: el patrón macro que conecta los items]

🔍 Para profundizar
- [link a paper / repo / hilo más denso]
```

### 4. Persistencia
- Guarda en `wiki/ai-news/{YYYY-MM-DD}.md` con frontmatter (tags: ai-news, daily-brief; sources: [...])
- Emite evento `ai_news_brief` con payload del resumen
- Si modo notify activo: enviar a Telegram (chat 574707975)

## Reglas

- **Idioma**: siempre español, tono directo, sin floritura.
- **Verificación**: cada claim debe tener link primario, no segunda mano.
- **Sin alucinación de fechas**: si no se puede confirmar la fecha del item, marcar como `(fecha aproximada)`.
- **Sin tweets sin engagement real** ni hilos sin link a fuente primaria.
- **Compacto**: máximo 600 palabras totales.

## Validación (obligatoria)

El output debe poder construirse como `claw_v2.ai_news.AiNewsBrief` y pasar
`validate_ai_news_brief(brief)` sin errores antes de enviarse al usuario.

**Jerarquía de evidencia:** `retrieved source metadata → claim evidence → model summary`. NO al revés. El modelo NO puede inventar `claim_map`; los claims se extraen de fuentes recolectadas (Firecrawl, WebSearch results, RSS, etc.).

Cada `ClaimEvidence` debe incluir:

| Campo | Requerido | Notas |
|-------|-----------|-------|
| `source_url` | ✅ | Link primario (no homepage) |
| `source_kind` | ✅ | `primary` / `wire` / `aggregator` / `social` / `unknown` |
| `published_at` | Recomendado | Si dice "de hoy", obligatorio |
| `fetched_at` | ✅ | Timestamp ISO de cuando recolectaste |
| `verified_fields` | Para `confidence=high` | No vacío |

**Reglas de confidence:**

- `high` requiere `source_kind ∈ {primary, wire}` Y `verified_fields` no vacío.
- `aggregator` sin fuente primaria asociada → tope `medium`.
- `social` sin corroboración → tope `low`.
- `unknown` no puede ser `high`.

**Sanity check de fecha:** el campo `weekday` del brief debe coincidir con el día calculado para `date` en `timezone`. Validador rechaza brief con weekday incorrecto (p.ej. "domingo" para `2026-04-27` que es lunes).

**Verification profile:** `ai_news_brief` requires evidence `("sources", "claim_map", "fetched_at")`.

## Cuándo escalar

- Si hay un anuncio de Anthropic/OpenAI/Google que afecta directamente Claw o el stack del usuario, marca con 🚨 al inicio del item y emite evento `ai_news_critical`.
- Si una capacidad nueva invalida una decisión de arquitectura previa (ver wiki), añadir nota `# acción sugerida: revisar [X]`.

## Schedule sugerido

`ScheduledSubAgentConfig(agent="alma", skill="ai-news-daily", interval_seconds=86400, lane="worker")`

Ideal: dispararse a las 5:00 AM CDT como parte del primer reporte del día.
