---
name: google-ads-manager
version: 2.0.0
category: marketing/paid-ads
description: >
  Diseña, optimiza y reporta campañas de Google Ads (Search, Display, Performance Max)
  para clientes de Pachano Design. Genera estructura de campaña completa, copy de anuncios,
  extensiones, bidding strategy y reporte de rendimiento con recomendaciones de optimización.
  Ejecuta análisis y entrega assets listos para implementar.
triggers:
  - "crea una campaña de Google Ads para"
  - "optimiza los anuncios de Google de"
  - "cómo están funcionando los ads de Google de"
  - "necesito anuncios de búsqueda para"
  - "Performance Max para"
  - "cliente con presupuesto para paid search"
  - "reporte semanal/mensual de Google Ads"
inputs:
  required:
    - client_name: Nombre del cliente
    - objective: "leads" | "ventas" | "awareness" | "tráfico"
    - monthly_budget: Presupuesto mensual en USD
    - target_url: URL de la landing page principal
  optional:
    - keyword_matrix: Output del skill keyword-intelligence
    - campaign_type: "search" | "display" | "pmax" | "all" (default: search)
    - existing_campaigns: Datos de campañas existentes para optimización
    - cpa_target: CPA objetivo en USD
    - competitor_ads: Anuncios de competidores para análisis
---

## Objective

Crear o optimizar campañas de Google Ads que maximicen el ROI del cliente con el presupuesto disponible.
Entregar assets 100% listos para implementar (copy aprobado, estructura, bidding) sin que el cliente tenga que rellenar blancos.

## Process

### Phase 1 — Campaign Architecture
1. Define tipo de campaña según objetivo y presupuesto:
   - Budget < $500/mes → Search focused, 1-2 ad groups
   - Budget $500-2k/mes → Search + remarketing Display
   - Budget > $2k/mes → Search + Display + Performance Max
2. Estructura de ad groups: 1 tema por grupo, 10-15 keywords exact/phrase match.
3. Separa siempre Brand vs Non-Brand en campañas distintas.
4. Define geographic targeting, ad schedule, device bid adjustments.

### Phase 2 — Keyword Setup
Si hay output de keyword-intelligence, importa Tier 1 transaccionales.
Si no, genera keywords basadas en:
- [client_business] + [servicio] + [geo]
- intención comercial: contratar/comprar/precio/cotizar + [servicio]

Asigna match types:
- Exact match: keywords de mayor intención
- Phrase match: variaciones de high intent
- Broad match modified: solo si presupuesto > $1k/mes con Smart Bidding

### Phase 3 — Ad Copy Creation
Por cada ad group, crea 3 Responsive Search Ads:
- 15 headlines (30 chars max cada uno) — variedad de beneficio, CTA, social proof
- 4 descriptions (90 chars max) — expandir beneficio, urgencia, diferenciador
- Pinned headlines: H1 = keyword principal, H2 = propuesta de valor

Extensiones obligatorias:
- Sitelinks: 4-6 páginas relevantes
- Callouts: 4-6 diferenciadores cortos
- Structured snippets: servicios/características
- Call extension: si el cliente quiere llamadas
- Lead form: si objetivo es leads

### Phase 4 — Bidding & Budget Strategy

| Objetivo | Fase | Estrategia |
|---|---|---|
| Leads | Primeras 2 semanas | Maximize Clicks (data gathering) |
| Leads | Semana 3+ con >30 conversiones | Target CPA |
| Ventas | Desde inicio si hay datos históricos | Target ROAS |
| Awareness | Siempre | Target Impression Share |

Distribución de presupuesto diario:
- Budget mensual / 30.4 = presupuesto diario
- Search: 70% del presupuesto
- Display/Remarketing: 20%
- PMax: 10% (si aplica)

### Phase 5 — Optimization Checklist (campañas existentes)
Scoring de campaña existente:

| Métrica | Umbral Óptimo | Acción si falla |
|---|---:|---|
| CTR Search | ≥ 5% | Revisar copy y relevancia |
| Quality Score | ≥ 7/10 | Mejorar landing + copy |
| Impression Share | ≥ 60% | Subir bids o budget |
| Conversion Rate | ≥ 3% | Optimizar landing |
| Cost/Conversion | ≤ CPA target | Pausar keywords ineficientes |
| Invalid Click Rate | ≤ 3% | Revisar IP exclusions |

## Output Format

### Para campaña nueva:

# Google Ads Setup — [Client Name]

**Fecha:** YYYY-MM-DD | **Objetivo:** [objective] | **Budget:** $X/mes

## Estructura de Campaña

### Campaña 1: [Nombre] — Non-Brand Search
- **Presupuesto diario:** $X
- **Bidding:** [estrategia]
- **Geo:** [targeting]
- **Schedule:** [días/horas]

#### Ad Group 1.1 — [Tema]
**Keywords:**
- [keyword] (exact)
- [keyword] (phrase)
- [keyword] (exact)

**Anuncio RSA 1:**
Headlines:
1. [headline] | 2. [headline] | 3. [headline] | [hasta 15]
Descriptions:
1. [description]
2. [description]
3. [description]
4. [description]

**Anuncio RSA 2:** [variante]
**Anuncio RSA 3:** [variante]

#### Ad Group 1.2 — [Tema]
[mismo formato]

### Extensiones
**Sitelinks:**
- [título] → [url] | [descripción] [×4-6]

**Callouts:** [diferenciador] | [diferenciador] | [diferenciador] | [diferenciador]

**Structured Snippets — Servicios:** [servicio1], [servicio2], [servicio3]

## Negative Keywords
- [keyword] — [razón]
[lista completa]

## KPIs y Metas Mes 1

| Métrica | Meta | Revisión |
|---|---|---|
| Impresiones | X | Semanal |
| CTR | ≥5% | Semanal |
| Conversiones | X | Semanal |
| CPA | ≤$X | Mensual |

## Acciones Semana 1
- [ ] Subir estructura a Google Ads — Dev/Media Buyer
- [ ] Verificar conversión tracking — Dev
- [ ] Activar campaña — Media Buyer
- [ ] Revisión primer reporte en 7 días — Pachano Design

### Para reporte/optimización:

# Reporte Google Ads — [Client Name]

**Período:** [fecha inicio] - [fecha fin]

## Resumen de Performance

| Métrica | Este período | Período anterior | Δ% |
|---|---:|---:|---:|
| Inversión | $X | $X | X% |
| Clics | X | X | X% |
| Conversiones | X | X | X% |
| CPA | $X | $X | X% |
| ROAS | X | X | X% |

## Alertas
- 🔴 CRITICAL: [problema que requiere acción inmediata]
- 🟡 WARN: [métrica fuera de rango]
- 🟢 OK: [métricas en orden]

## Top 5 Keywords (por conversiones)
[tabla]

## Bottom 5 Keywords (candidatas a pausa)
[tabla con razón]

## Optimizaciones Aplicadas Esta Semana
- [acción tomada] → [resultado]

## Recomendaciones Próxima Semana
1. [ ] [acción específica] — responsable — impacto esperado
2. [ ] [acción específica] — responsable — impacto esperado
3. [ ] [acción específica] — responsable — impacto esperado

## Anti-Patterns
- ❌ No lanzar sin conversion tracking verificado — campaña sin datos de conversión es dinero quemado.
- ❌ No mezclar Brand y Non-Brand en misma campaña — distorsiona métricas y bidding.
- ❌ No usar Broad Match sin Smart Bidding y datos históricos — genera tráfico irrelevante.
- ❌ No crear anuncios sin extensiones — pierdes espacio visual y quality score.
- ❌ No reportar solo impresiones y clics — el cliente necesita saber cuántos leads/ventas generó.
- ❌ No cambiar bids y copy en la misma semana — no sabrás qué mejoró el performance.

## Done Criteria

Setup nuevo:
- [ ] Mínimo 2 ad groups con 3 RSAs cada uno.
- [ ] 15 headlines y 4 descriptions por RSA.
- [ ] Negative keyword list con mínimo 15 términos.
- [ ] Todas las extensiones configuradas (mínimo: sitelinks, callouts, snippets).
- [ ] Bidding strategy justificada según presupuesto y data disponible.
- [ ] Conversion tracking especificado.

Reporte/Optimización:
- [ ] Comparación período anterior incluida.
- [ ] Alertas clasificadas (CRITICAL/WARN/OK).
- [ ] Mínimo 3 recomendaciones con owner asignado.
- [ ] Keywords candidatas a pausa identificadas.
