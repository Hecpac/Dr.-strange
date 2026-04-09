---
name: keyword-intelligence
version: 2.0.0
category: marketing/seo
description: >
  Investiga, clasifica y mapea keywords estratégicas para SEO, AEO y campañas de paid ads.
  Produce keyword matrix lista para usar en contenido, landing pages y grupos de anuncios.
  Se activa cuando se necesita definir estrategia de keywords para un cliente nuevo o al lanzar una campaña.
triggers:
  - "qué keywords debería atacar"
  - "investiga las palabras clave de"
  - "keywords para la campaña de"
  - "análisis de keywords del nicho"
  - "qué busca el cliente ideal de"
  - "cliente nuevo onboarding"
  - "antes de crear anuncios de Google/Meta/LinkedIn"
inputs:
  required:
    - client_name: Nombre del cliente
    - industry: Industria o nicho (ej. “seguros de auto Texas”)
    - target_audience: Descripción del cliente ideal
  optional:
    - seed_keywords: Keywords semilla proporcionadas por el cliente
    - competitors: URLs competidoras para keyword gap
    - budget_type: "seo" | "paid" | "both" (default: both)
    - location: Geografía objetivo (ej. “Texas, USA”)
---

## Objective

Producir una keyword matrix priorizada que sirva simultáneamente como base para estrategia SEO/AEO, contenido y grupos de anuncios paid.
Cada keyword debe tener suficiente contexto para que un redactor o media buyer pueda actuar sin investigación adicional.

## Process

### Phase 1 — Seed Expansion
1. A partir de las seeds, genera 3 capas de expansión:
   - Head keywords: 1-2 palabras, alto volumen, alta competencia
   - Body keywords: 2-3 palabras, volumen medio, intención clara
   - Long-tail keywords: 4+ palabras, bajo volumen, alta conversión
2. Para AEO: identifica variantes conversacionales (“cómo”, “qué es”, “cuál es el mejor”, “para qué sirve”).
3. Para paid: identifica keywords comerciales/transaccionales (“comprar”, “precio”, “contratar”, “cotizar”).

### Phase 2 — Intent Classification
Clasifica cada keyword por intención:
- I = Informacional (AEO/Blog) — “qué es el seguro de responsabilidad civil”
- N = Navegacional (Brand) — “pachano design web”
- C = Comercial (Paid/Landing) — “agencia SEO Colombia precios”
- T = Transaccional (Paid alta prioridad) — “contratar agencia SEO Bogotá”

### Phase 3 — Scoring Matrix
Puntúa para priorización:

| Dimensión | Peso | Criterio |
|---|---:|---|
| Intención comercial | 35% | T=10, C=7, I=4, N=2 |
| Volumen mensual | 25% | >10k=10, 1-10k=7, 100-1k=5, <100=3 |
| Dificultad SEO (inversa) | 20% | KD<20=10, 20-40=7, 40-60=5, >60=2 |
| Relevancia al negocio | 20% | Score 1-10 manual |

- Score ≥ 7.5 → Tier 1 (atacar primero)
- Score 5-7.4 → Tier 2 (segundo trimestre)
- Score < 5 → Tier 3 (largo plazo)

### Phase 4 — Mapping
Asigna cada keyword Tier 1 y 2 a:
- URL de destino (existente o a crear)
- Canal: SEO / Google Ads / Meta / LinkedIn / AEO
- Tipo de contenido: landing page, blog post, FAQ, anuncio

## Output Format

# Keyword Intelligence — [Client Name]

**Fecha:** YYYY-MM-DD | **Nicho:** [industry] | **Geo:** [location]

## Resumen
- Total keywords analizadas: N
- Tier 1 (acción inmediata): N keywords
- Tier 2 (Q2): N keywords
- Oportunidad AEO detectada: N queries conversacionales

## Keyword Matrix Tier 1

| Keyword | Vol/mes | KD | Intent | Score | Canal | URL Destino |
|---|---:|---:|---|---:|---|---|
| contratar agencia SEO bogotá | 880 | 28 | T | 8.4 | Google Ads + SEO | /servicios/seo |
| [keyword] | | | | | | |

## Keyword Matrix Tier 2
[Mismo formato]

## AEO Opportunities — Queries Conversacionales
Estas keywords son ideales para schema FAQ, blog posts en formato pregunta-respuesta y citabilidad por AI:

| Query | Intención AEO | Formato Recomendado |
|---|---|---|
| ¿cuánto cuesta una agencia de SEO en Colombia? | Precio/comparación | FAQ Schema + tabla de precios |
| [query] | | |

## Negative Keywords (para Paid Ads)
Keywords a excluir en campañas para evitar clics no calificados:
- [keyword negativa] — razón
- [keyword negativa] — razón

## Grupos de Anuncios Sugeridos (Google Ads)
- **Grupo 1 — [Tema]**: keyword1, keyword2, keyword3
- **Grupo 2 — [Tema]**: keyword1, keyword2, keyword3

## Próximos Pasos
1. [ ] Validar Tier 1 con cliente — 30 min
2. [ ] Asignar keywords a páginas existentes o crear brief de nuevas
3. [ ] Importar Tier 1 transaccionales a Google Ads como exact/phrase match

## Anti-Patterns
- ❌ No entregar listas sin scoring — más keywords no es mejor; priorización es el valor.
- ❌ No ignorar negativas — sin negative keywords la campaña paid quema presupuesto.
- ❌ No mapear múltiples keywords al mismo URL sin diferenciación — crea canibalización.
- ❌ No omitir queries conversacionales — son la mayor oportunidad AEO actual.
- ❌ No usar volumen como único criterio — una keyword de 50/mes con intent T puede convertir mejor que una de 10k informacional.

## Done Criteria
- [ ] Mínimo 20 keywords Tier 1 identificadas.
- [ ] 100% de Tier 1 tienen URL de destino asignada.
- [ ] Negative keyword list con mínimo 10 términos.
- [ ] Grupos de anuncios sugeridos (mínimo 3).
- [ ] AEO opportunities separadas y con formato recomendado.
- [ ] Output listo para importar a Google Ads (formato tabla).
