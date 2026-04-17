---
name: meta-ads-manager
version: 2.0.0
category: marketing/paid-ads
description: >
  Diseña, optimiza y reporta campañas de Meta Ads (Facebook + Instagram)
  para clientes de Pachano Design. Crea estructura completa de campaña, copy de anuncios,
  audiencias, creative briefs y análisis de rendimiento. Especializado en campañas
  de generación de leads, conversiones y awareness para B2B y B2C.
triggers:
  - "crea anuncios de Facebook para"
  - "campaña de Instagram para"
  - "Meta Ads para"
  - "anuncios en redes sociales para"
  - "cómo están los Facebook Ads de"
  - "necesito generar leads con Meta para"
  - "reporte semanal/mensual de Meta Ads"
inputs:
  required:
    - client_name: Nombre del cliente
    - objective: "leads" | "conversiones" | "awareness" | "tráfico" | "engagement"
    - monthly_budget: Presupuesto mensual en USD
    - target_audience: Descripción del cliente ideal (edad, intereses, comportamientos)
  optional:
    - product_service: Servicio o producto a promocionar
    - existing_assets: Imágenes, videos o copy existentes
    - pixel_data: Si hay Pixel instalado con datos históricos
    - existing_audiences: Custom audiences ya creadas
    - competitor_pages: Páginas de competidores para análisis de audiencia
---

## Objective

Crear campañas de Meta Ads que generen resultados medibles (leads, ventas, awareness) para clientes de Pachano Design,
con assets de copy y creative brief listos para producción. El output debe ser ejecutable sin reuniones adicionales.

## Process

### Phase 1 — Audience Architecture

#### Audiencias Frías (prospecting)
Construye 3 audiencias distintas para A/B testing:
1. Interés amplio: categorías de interés relevantes al negocio, sin restricción excesiva.
2. Interés específico: comportamientos de compra + intereses de nicho.
3. Lookalike: 1-3% similar a lista de clientes (si existe pixel/lista).

#### Audiencias Calientes (retargeting)
- Visitantes web: 30 días (pixel requerido)
- Engagement: interactuaron con página/cuenta últimos 60 días
- Lead list: subida de emails de clientes existentes

Regla de exclusión obligatoria: siempre excluir clientes actuales de prospecting.

#### Sizing Guide

| Budget | Audiencia mínima recomendada |
|---|---|
| < $500/mes | 500k - 2M personas |
| $500-2k/mes | 1M - 5M personas |
| > $2k/mes | 2M - 10M personas |

### Phase 2 — Campaign Structure (TOFU/MOFU/BOFU)

Campaign Level — Objetivo de conversión
├── Ad Set 1 — Audiencia Fría 1 (Interés amplio)
│   ├── Ad 1 — Formato video (15s)
│   ├── Ad 2 — Formato imagen estática
│   └── Ad 3 — Formato carrusel
├── Ad Set 2 — Audiencia Fría 2 (Interés específico)
│   └── [mismos 3 formatos]
└── Ad Set 3 — Retargeting Visitantes Web
    ├── Ad 1 — Mensaje de seguimiento / objeción handling
    └── Ad 2 — Oferta/urgencia

### Phase 3 — Creative Brief & Copy
Por cada ad, entrega:
- Hook (primeras 3 palabras del copy — detienen el scroll)
- Copy principal (texto del anuncio, 125 chars visible sin “ver más”)
- Headline (título del anuncio, 27 chars max)
- Description (subtítulo, 27 chars max)
- CTA button: Learn More / Sign Up / Get Quote / Shop Now
- Visual brief: descripción exacta de la imagen/video a producir (colores, personas, texto en imagen, mood)

Estructura de copy que convierte:
1. Hook (problema o promesa)
2. Agitación (consecuencia)
3. Solución
4. Social proof
5. CTA

### Phase 4 — Bidding & Budget Allocation

| Objetivo | Estrategia de puja |
|---|---|
| Leads | Lowest Cost (primero) → Cost Cap cuando CPA estabilice |
| Conversiones | Lowest Cost → Target Cost con 50+ eventos/semana |
| Awareness | CPM — Reach |
| Tráfico | Link Click → Landing Page Views |

Distribución recomendada:
- 70% prospecting (audiencias frías)
- 30% retargeting

### Phase 5 — Optimization Rules (campañas existentes)
Scoring de ad sets:

| Métrica | Umbral Óptimo | Acción |
|---|---|---|
| CTR (link) | ≥ 1% | Si <1%: pausar y renovar creative |
| CPM | ≤ $15 (varía por industria) | Si alto: ampliar audiencia |
| Frecuencia | ≤ 3.5 | Si >3.5: rotar creative o nueva audiencia |
| CPL/CPA | ≤ target | Si alto: revisar landing o audiencia |
| ROAS | ≥ 3x (e-commerce) | Si bajo: optimizar funnel |
| Relevance Score | ≥ 6/10 | Si bajo: mejorar match audience-copy |

Regla de los 7 días:
No pausar ad set antes de 7 días y 500+ impresiones — el algoritmo necesita tiempo de aprendizaje.

## Output Format

### Para campaña nueva:

# Meta Ads Setup — [Client Name]

**Fecha:** YYYY-MM-DD | **Objetivo:** [objective] | **Budget:** $X/mes

## Estructura de Campaña

### Campaña: [Nombre] ([Objetivo Meta])
- **Presupuesto:** $X/día (campaign budget optimization)
- **Pixel:** [verificado/pendiente]
- **Attribution:** 7-day click, 1-day view

---
#### Ad Set 1 — [Nombre audiencia]
**Audiencia:**
- Edad: X-X años
- Ubicación: [geo]
- Intereses: [interés1], [interés2], [interés3]
- Comportamientos: [comportamiento]
- Excluir: [exclusiones]
- Tamaño estimado: X-XM personas

**Presupuesto ad set:** $X/día
**Placements:** Automatic (recomendado) / Feed only

##### Ad 1.1 — [Formato: Video/Imagen/Carrusel]
- **Hook:** "[primeras palabras que detienen el scroll]"
- **Copy principal:** [texto completo del anuncio — máx 125 chars para que no se corte]
- **Headline:** [27 chars max]
- **Description:** [27 chars max]
- **CTA:** [botón]
- **URL:** [landing page]
- **Visual Brief:**
  - Formato: [9:16 para stories/reels | 1:1 para feed | 1.91:1 para feed horizontal]
  - Contenido: [descripción exacta: personas, escenario, texto en imagen, colores]
  - Texto en imagen: "[máx 20% del área]"
  - Mood: [profesional/energético/cálido/urgente]

##### Ad 1.2 — [Formato]
[mismo formato]

---
#### Ad Set 2 — Retargeting Visitantes Web
[mismo formato con copy adaptado a personas que ya conocen el producto]

---
## Negative Audiences
- Clientes actuales (lista de emails)
- [otras exclusiones relevantes]

## KPIs y Metas Mes 1

| Métrica | Meta | Revisión |
|---|---|---|
| CPL / CPA | $X | Semanal |
| CTR | ≥1% | Diario |
| Leads generados | X | Semanal |
| ROAS | X | Mensual |

## Checklist Pre-Launch
- [ ] Pixel instalado y verificado
- [ ] Evento de conversión configurado (Lead/Purchase)
- [ ] Todos los assets creativos producidos
- [ ] Landing page revisada (mobile first)
- [ ] UTM parameters configurados

### Para reporte/optimización:

# Reporte Meta Ads — [Client Name]

**Período:** [fecha inicio] - [fecha fin]

## Performance Overview

| Métrica | Este período | Anterior | Δ% |
|---|---:|---:|---:|
| Inversión | $X | $X | X% |
| Impresiones | X | X | X% |
| Clics | X | X | X% |
| CTR | X% | X% | X% |
| Conversiones | X | X | X% |
| CPL/CPA | $X | $X | X% |

## Estado por Ad Set

| Ad Set | Gasto | Leads | CPL | Frecuencia | Estado |
|---|---:|---:|---:|---:|---|
| [nombre] | $X | X | $X | X | 🟢/🟡/🔴 |

## Creative Performance

| Ad | CTR | CPL | Frecuencia | Acción |
|---|---:|---:|---:|---|
| [nombre] | X% | $X | X | Escalar/Rotar/Pausar |

## Alertas
- 🔴 [problema crítico]
- 🟡 [advertencia]
- 🟢 [funcionando bien]

## Optimizaciones Esta Semana
- [acción] → [resultado]

## Plan Próxima Semana
1. [ ] [acción] — responsable

## Anti-Patterns
- ❌ No lanzar sin Pixel y evento de conversión verificados — sin tracking no hay optimización posible.
- ❌ No crear audiencias menores a 500k — el algoritmo no tiene suficiente espacio para optimizar.
- ❌ No cambiar ad sets en fase de aprendizaje — respetar los 7 días mínimo y 50 eventos de conversión.
- ❌ No usar el mismo copy para prospecting y retargeting — el contexto del usuario es diferente.
- ❌ No ignorar frecuencia — >3.5 = fatiga de audiencia = costo sube, resultados bajan.
- ❌ No reportar solo “me gustas” — KPIs deben ser conversiones y CPL/CPA, no engagement.

## Done Criteria

Setup nuevo:
- [ ] Mínimo 2 ad sets (1 frío + 1 retargeting).
- [ ] 3 ads por ad set con formatos distintos.
- [ ] Visual brief detallado para cada creative (producible sin reunión).
- [ ] Pixel y conversión tracking especificados.
- [ ] Exclusiones de audiencia configuradas.
- [ ] UTM parameters definidos.

Reporte/Optimización:
- [ ] Comparación período anterior.
- [ ] Estado de cada ad set con semáforo.
- [ ] Creative performance con recomendación (escalar/rotar/pausar).
- [ ] Alertas clasificadas.
- [ ] Próximos pasos con owner.
