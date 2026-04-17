---
name: linkedin-ads-manager
version: 2.0.0
category: marketing/paid-ads
description: >
  Diseña, optimiza y reporta campañas de LinkedIn Ads para clientes B2B de Pachano Design.
  Crea estructura de campañas (Lead Gen / Website Conversions / Document Ads), segmentación
  por cargo/industria/tamaño de empresa, copy de anuncios, y plan de optimización enfocado
  en calidad de lead y CPL.
triggers:
  - "crea campaña de LinkedIn Ads para"
  - "optimiza LinkedIn Ads de"
  - "necesito leads B2B en LinkedIn para"
  - "reporte de LinkedIn Ads"
  - "campaña de generación de demanda en LinkedIn"
inputs:
  required:
    - client_name: Nombre del cliente
    - objective: "leads" | "conversiones" | "awareness"
    - monthly_budget: Presupuesto mensual en USD
    - target_audience_b2b: ICP (industria, tamaño empresa, cargos, seniority, geo)
    - target_url: Landing o formulario destino
  optional:
    - offer: Oferta principal (demo, diagnóstico, guía, checklist)
    - existing_campaigns: Datos de campañas existentes
    - cpl_target: CPL objetivo
    - crm_feedback: Calidad de leads históricos por segmento
---

## Objective

Construir y operar campañas de LinkedIn Ads orientadas a pipeline B2B rentable,
maximizando calidad de lead por encima de volumen bruto.

## Process

### Phase 1 — Qualification Gate
- Si `monthly_budget < 1500`, recomendar no activar LinkedIn y escalar a orquestador.
- Si budget ≥ 1500, proceder con estructura completa.

### Phase 2 — Campaign Architecture
- Separar por objetivo: TOFU (awareness), MOFU (consideration), BOFU (lead gen/conversion).
- Crear 2-3 campañas máximo por objetivo para evitar dispersión.
- Estructura por campaña:
  - 2-4 ad sets (audiencias distintas)
  - 2-3 anuncios por ad set

### Phase 3 — Audience Design (B2B)
Segmentación base:
- Industria
- Tamaño de empresa
- Cargo / función
- Seniority
- Ubicación

Reglas:
- Excluir empleados del cliente y clientes actuales.
- Crear audiencia retargeting de visitantes y engagers.
- Evitar audiencias demasiado estrechas (<20k) salvo BOFU ultra específico.

### Phase 4 — Creative & Copy
Por cada anuncio entregar:
- Hook (primeras 6-10 palabras)
- Body copy (dolor → solución → prueba → CTA)
- Headline
- CTA (Book demo / Download / Learn more)
- Recurso visual recomendado (single image / carousel / document ad / short video)

### Phase 5 — Bidding & Optimization
- Inicio: Max Delivery/Lowest Cost para aprendizaje.
- Con data suficiente: cost cap (si CPL estable y volumen suficiente).

Métricas clave:
- CTR objetivo: ≥ 0.60% (depende del nicho)
- Lead form completion rate: ≥ 10-15%
- CPL: <= target
- Lead quality score (CRM): tendencia estable o al alza

## Output Format

# LinkedIn Ads Setup — [Client Name]
**Fecha:** YYYY-MM-DD | **Objetivo:** [objective] | **Budget:** $X/mes

## Arquitectura
- Campañas: [lista]
- Ad sets por campaña: [N]
- Anuncios por ad set: [N]

## Audiencias
| Ad Set | Segmentación | Tamaño estimado | Exclusiones |
|---|---|---:|---|
| [nombre] | [industria+cargo+seniority] | X | [lista] |

## Anuncios (por ad set)
### Ad 1 — [formato]
- Hook: "..."
- Body: "..."
- Headline: "..."
- CTA: [button]
- URL/Form: [destino]

## Presupuesto y puja
- Presupuesto diario: $X
- Estrategia de puja: [estrategia]
- KPI primario: [CPL / SQL rate]

## Reporte de Optimización (si aplica)
| Métrica | Actual | Anterior | Estado |
|---|---:|---:|---|
| Gasto | $X | $X | 🔴🟡🟢 |
| Leads | X | X | 🔴🟡🟢 |
| CPL | $X | $X | 🔴🟡🟢 |
| Quality lead score | X | X | 🔴🟡🟢 |

## Próximas acciones
1. [ ] [acción] — owner — fecha
2. [ ] [acción] — owner — fecha
3. [ ] [acción] — owner — fecha

## Anti-Patterns
- ❌ Activar LinkedIn con presupuesto insuficiente.
- ❌ Optimizar solo por CTR sin validar calidad de lead.
- ❌ No excluir audiencias ya convertidas.
- ❌ Mezclar múltiples ICPs incompatibles en el mismo ad set.

## Done Criteria
- [ ] Segmentación B2B completa y justificada.
- [ ] Mínimo 2 ad sets y 2 anuncios por ad set.
- [ ] Copy + CTA + formato listos para publicar.
- [ ] KPI objetivo y plan de optimización definidos.
- [ ] Si budget insuficiente, escalado documentado.
