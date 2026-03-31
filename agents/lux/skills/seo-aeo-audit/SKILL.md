---
name: seo-aeo-audit
version: 2.0.0
category: marketing/seo
description: >
  Audita la salud SEO y AEO (Answer Engine Optimization) de un sitio cliente.
  Clasifica hallazgos en critical/warn/info, prioriza por impacto y genera
  reporte ejecutivo con plan de acción en 30/60/90 días.
triggers:
  - "audita el SEO de"
  - "revisa el posicionamiento de"
  - "analiza el sitio de"
  - "hay problemas de indexación en"
  - "cómo está el AEO de"
  - "auditoría técnica de"
  - "cliente menciona caída de tráfico orgánico"
  - "cliente menciona problemas de visibilidad en AI search"
inputs:
  required:
    - url: URL raíz del sitio (ej. https://ejemplo.com)
    - client_name: Nombre del cliente
  optional:
    - competitors: Lista de URLs competidoras (max 3)
    - target_keywords: Keywords estratégicas del cliente
    - gsc_data: Datos de Google Search Console (CSV o JSON)
    - ga_data: Datos de Google Analytics
---

## Objective

Entregar un diagnóstico SEO/AEO completo, accionable y priorizado para sitios de clientes de Pachano Design.
El reporte debe ser entendible por el cliente (sin jerga técnica) y ejecutable por el equipo técnico.

## Process

### Phase 1 — Technical SEO Crawl (auto)
1. Verifica indexabilidad: robots.txt, sitemap.xml, meta robots, canonical tags.
2. Analiza Core Web Vitals: LCP, INP, CLS vía PageSpeed API.
3. Detecta errores críticos: páginas 4xx/5xx, redirect chains, hreflang broken.
4. Revisa estructura: H1-H6 hierarchy, meta titles/descriptions, schema markup.
5. Evalúa mobile-first: viewport, touch targets, font sizes.

### Phase 2 — AEO Analysis (Answer Engine Optimization)
1. Detecta presencia en featured snippets y PAA boxes.
2. Analiza estructura para LLM crawlability: FAQ schema, HowTo schema, Article schema.
3. Evalúa si el contenido responde preguntas conversacionales directamente.
4. Verifica llms.txt o robots.txt con reglas para AI crawlers (GPTBot, ClaudeBot, PerplexityBot).
5. Identifica oportunidades de citabilidad por AI: definiciones claras, datos estructurados, autoría visible.

### Phase 3 — Content & Authority Audit
1. Top 10 páginas por tráfico orgánico estimado.
2. Keyword gaps vs competidores (si se proporcionan).
3. Backlink profile: DA/DR, spam score, link velocity.
4. Content freshness: páginas sin actualizar >12 meses en posiciones 4-20.

### Phase 4 — Scoring & Prioritization
Puntúa cada hallazgo:

| Dimensión | Peso | Escala |
|---|---:|---|
| Impacto en tráfico | 40% | 1-10 |
| Facilidad de implementación | 30% | 1-10 (10=fácil) |
| Urgencia técnica | 30% | 1-10 |

Score final = (Impacto×4 + Facilidad×3 + Urgencia×3) / 10
- Score ≥ 8: CRITICAL — bloquea crecimiento
- Score 5-7.9: WARN — limita potencial
- Score < 5: INFO — mejora incremental

## Output Format

# Reporte SEO/AEO — [Client Name]

**Fecha:** YYYY-MM-DD | **Auditor:** Pachano Design | **Versión:** 1.0

## Resumen Ejecutivo
[2-3 oraciones: estado actual, principal problema, oportunidad más grande]

## Dashboard de Salud

| Categoría | Estado | Score |
|---|---|---:|
| Indexabilidad | 🔴/🟡/🟢 | X/10 |
| Core Web Vitals | 🔴/🟡/🟢 | X/10 |
| AEO / AI Visibility | 🔴/🟡/🟢 | X/10 |
| Contenido | 🔴/🟡/🟢 | X/10 |
| Autoridad | 🔴/🟡/🟢 | X/10 |

**Score Global: X/10**

## Hallazgos CRITICAL [N items]

### CRIT-01: [Título del problema]
- **Qué pasa:** [descripción sin jerga]
- **Impacto:** [consecuencia concreta en tráfico/visibilidad]
- **Fix:** [acción específica]
- **Esfuerzo:** [tiempo estimado]
- **Score:** X.X/10

## Hallazgos WARN [N items]
[Mismo formato]

## Hallazgos INFO [N items]
[Mismo formato]

## Plan de Acción 30/60/90 días

### 30 días — Quick Wins
- [ ] Fix CRIT-01: [acción] — Responsable: Dev
- [ ] Fix CRIT-02: [acción] — Responsable: Content

### 60 días — Fundación
- [ ] [acciones WARN de mayor score]

### 90 días — Crecimiento
- [ ] [acciones INFO + estrategia AEO]

## KPIs de Seguimiento

| Métrica | Baseline | Meta 90d | Herramienta |
|---|---:|---:|---|
| Posición media | X | X | GSC |
| Clics orgánicos/mes | X | X | GSC |
| AI citations | X | X | Manual/Perplexity |
| Core Web Vitals (LCP) | Xs | <2.5s | PSI |

## Anti-Patterns
- ❌ No listar problemas sin score — sin priorización el cliente no sabe por dónde empezar.
- ❌ No incluir jerga técnica sin explicación — “canonical loop” → “páginas que se apuntan entre sí, confundiendo a Google”.
- ❌ No hacer recomendaciones genéricas — “mejora tu contenido” → “actualiza /servicios/diseno-web con datos de 2025 y agrega FAQ schema”.
- ❌ No ignorar AEO — el 30%+ de búsquedas ahora pasan por AI; es oportunidad diferenciadora de Pachano Design.
- ❌ No reportar sin baseline — siempre captura el estado actual antes de proponer metas.

## Done Criteria
- [ ] Score global calculado con las 5 categorías.
- [ ] Al menos 3 CRITICAL identificados o se confirma que no existen.
- [ ] Cada hallazgo tiene fix específico con tiempo estimado.
- [ ] Plan 30/60/90 días tiene owners asignados.
- [ ] KPIs de seguimiento con baseline real capturado.
- [ ] Reporte entendible por cliente no técnico (test: ¿puede leerlo sin preguntar?).
