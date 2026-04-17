---
name: competitor-spy
version: 2.0.0
category: marketing/intelligence
description: >
  Analiza la estrategia de marketing digital de los competidores de un cliente:
  keywords que atacan, anuncios que corren, contenido que producen, y posicionamiento SEO.
  Genera inteligencia accionable para identificar oportunidades y amenazas.
  Se ejecuta al onboardear un cliente nuevo o mensualmente como monitoreo.
triggers:
  - "qué están haciendo los competidores de"
  - "analiza la competencia de"
  - "espionaje de competidores para"
  - "qué keywords usan mis competidores"
  - "qué anuncios corren los competidores de"
  - "onboarding de cliente nuevo"
  - "reporte mensual de inteligencia competitiva"
inputs:
  required:
    - client_name: Nombre del cliente
    - client_url: URL del cliente
    - competitor_urls: Lista de URLs competidoras (max 5)
  optional:
    - channels: ["ads", "seo", "content", "social"] — qué analizar
    - keywords_client: Keywords que ya ataca el cliente
---

## Objective

Mapear la estrategia de marketing digital de los competidores para identificar:
1) qué está funcionando para ellos que podemos replicar/superar,
2) qué gaps existen que el cliente puede explotar,
3) qué amenazas se aproximan.

## Process

### Phase 1 — SEO Competitor Analysis
Por cada competidor:
1. Domain Authority / Rating estimado.
2. Top 10 páginas por tráfico orgánico estimado.
3. Keywords en top 10 que el cliente NO tiene.
4. Keywords donde ambos compiten (battlefield keywords).
5. Estructura del sitio: secciones, blog, recursos.

### Phase 2 — Paid Ads Intelligence

**Google Ads:**
- ¿Están corriendo Google Ads? (verificar con herramienta de transparencia).
- Keywords que aparecen en sus anuncios.
- Copy de anuncios: propuesta de valor, CTAs, diferenciadores.
- Estimado de gasto mensual.

**Meta Ads:**
- Verificar Meta Ad Library (público y gratuito).
- Formatos usados: imagen, video, carrusel.
- Ángulos de copy: precio, beneficio, social proof, urgencia.
- Duración de los anuncios (>30 días = anuncio que convierte).

**LinkedIn Ads:**
- Verificar posts patrocinados en la página de empresa.
- Formatos y mensajes usados.

### Phase 3 — Content & AEO Analysis
1. Frecuencia de publicación en blog.
2. Temas cubiertos vs no cubiertos.
3. ¿Tienen schema markup? ¿Aparecen en featured snippets?
4. ¿Son citados por AI engines? (verificar en Perplexity).
5. Tipo de contenido que más comparte su audiencia.

### Phase 4 — Gap & Opportunity Matrix

| Tipo | Descripción | Prioridad |
|---|---|---|
| Keyword Gap | Keywords donde competidor rankea pero cliente no | Alta |
| Content Gap | Temas que competidor cubre, cliente no | Media |
| Ad Gap | Ángulos de anuncio no usados por nadie | Alta |
| AEO Gap | Preguntas respondidas por AI que nadie ocupa bien | Alta |
| Weakness | Área donde competidor es débil | Alta |

### Phase 5 — Threat Assessment
Clasifica a cada competidor:

| Nivel | Criterio |
|---|---|
| 🔴 THREAT | >3× el tráfico del cliente, presupuesto ads visible, creciendo |
| 🟡 WATCH | Similar tamaño, activo en ads y SEO |
| 🟢 BEATABLE | Menor tráfico, poca actividad en paid, contenido desactualizado |

## Output Format

# Inteligencia Competitiva — [Client Name]

**Fecha:** YYYY-MM-DD | **Competidores analizados:** X

---
## Resumen Ejecutivo
[2-3 oraciones: quién lidera el espacio, cuál es la mayor amenaza, cuál es la oportunidad más grande]

---
## Mapa Competitivo

| Competidor | DA/DR | Tráfico est./mes | Activo en Ads | Nivel de Amenaza |
|---|---:|---:|---|---|
| [url] | X | Xk | Sí/No | 🔴🟡🟢 |
| [url] | X | Xk | Sí/No | 🔴🟡🟢 |

---
## Análisis por Competidor

### Competidor 1: [nombre] — [url]
**Nivel:** 🔴🟡🟢 [THREAT/WATCH/BEATABLE]

**SEO:**
- Top keywords: [keyword] (pos. X, ~X clics/mes), [keyword], [keyword]
- Fortaleza: [por qué rankea bien]
- Debilidad SEO: [dónde falla]

**Google Ads:**
- Estado: [activo/inactivo]
- Keywords visibles: [keyword], [keyword]
- Copy más usado: "[headline]" → "[descripción]"
- Propuesta de valor: [qué prometen]
- Estimado gasto: $X-Xk/mes

**Meta Ads (via Ad Library):**
- Anuncios activos: X
- Formato dominante: [imagen/video/carrusel]
- Anuncio más antiguo (=más exitoso): [descripción + ángulo]
- Ángulos usados: [precio/urgencia/social proof/educativo]

**Contenido:**
- Frecuencia: X posts/mes
- Temas principales: [tema1], [tema2]
- AEO: [¿son citados por AI? ¿tienen featured snippets?]

**Cómo superarlos:**
- [acción específica 1]
- [acción específica 2]

---
## Keyword Battlefield
Keywords donde ambos (cliente y competidores) compiten:

| Keyword | Pos. Cliente | Pos. Competidor | Vol/mes | Acción |
|---|---:|---:|---:|---|
| [keyword] | X | X | X | [optimizar/atacar/defender] |

---
## Keyword Gaps — Oportunidades para el Cliente
Keywords donde competidores rankean y el cliente NO:

| Keyword | Competidor | Pos. Comp. | Vol/mes | Dificultad | Prioridad |
|---|---|---:|---:|---:|---|
| [keyword] | [comp] | X | X | X | Alta/Media |

---
## Ad Gaps — Ángulos No Explotados
Ángulos de anuncio que nadie en el mercado está usando:
1. [ángulo] — Justificación: [por qué resonaría]
2. [ángulo] — Justificación
3. [ángulo] — Justificación

---
## AEO Gaps — Preguntas Sin Dueño
Queries conversacionales donde ningún competidor está bien posicionado:

| Query | Respuesta actual en AI | Oportunidad |
|---|---|---|
| [pregunta] | [quién responde y qué tan bien] | [cómo tomarlo] |

---
## Plan de Acción — Explotar Ventaja Competitiva

| Prioridad | Acción | Tipo | Impacto | Tiempo estimado |
|---:|---|---|---|---|
| 1 | [acción específica] | SEO/Ads/Content | Alto | X semanas |
| 2 |  |  |  |  |
| 3 |  |  |  |  |

---
## Alertas de Amenaza
- 🔴 [amenaza inmediata a monitorear]
- 🟡 [tendencia a vigilar]

## Anti-Patterns
- ❌ No hacer análisis sin verificar Meta Ad Library — es gratuita y pública, no usarla es dejar dinero sobre la mesa.
- ❌ No reportar solo qué hace la competencia sin “cómo superarlos” — la inteligencia sin acción no tiene valor.
- ❌ No asumir que el competidor más grande es el más peligroso — uno más ágil y enfocado puede ser mayor amenaza.
- ❌ No ignorar AEO gaps — son la oportunidad menos explotada en la mayoría de industrias hoy.
- ❌ No actualizar menos de una vez al mes — el landscape de ads cambia rápido.

## Done Criteria
- [ ] Todos los competidores clasificados con nivel de amenaza justificado.
- [ ] Meta Ad Library verificada para cada competidor.
- [ ] Keyword gap list con mínimo 10 oportunidades.
- [ ] Ad gaps con mínimo 3 ángulos no explotados.
- [ ] AEO gaps identificados.
- [ ] Plan de acción con prioridades claras.
- [ ] Reporte entendible por el cliente sin conocimiento técnico de marketing.
