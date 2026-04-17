---
name: campaign-reporter
version: 2.0.0
category: marketing/reporting
description: >
  Genera reportes unificados de performance cross-channel (Google Ads + Meta + LinkedIn + SEO orgánico)
  para clientes de Pachano Design. Consolida datos, detecta anomalías, calcula ROI total y genera
  recomendaciones priorizadas. Puede ejecutarse semanal o mensualmente de forma autónoma.
triggers:
  - "reporte de marketing de"
  - "cómo están las campañas de"
  - "resumen de performance de"
  - "ROI de las campañas de"
  - "dashboard de marketing de"
  - "fin de semana (reporte semanal automático)"
  - "primer lunes del mes (reporte mensual)"
inputs:
  required:
    - client_name: Nombre del cliente
    - period: "weekly" | "monthly" | "quarterly"
    - channels: Lista de canales activos ["google", "meta", "linkedin", "seo"]
  optional:
    - google_data: Métricas de Google Ads del período
    - meta_data: Métricas de Meta Ads del período
    - linkedin_data: Métricas de LinkedIn Ads del período
    - seo_data: Datos de GSC/GA del período
    - revenue_data: Ingresos atribuibles para calcular ROAS real
---

## Objective

Entregar un reporte ejecutivo que le diga al cliente exactamente qué está funcionando, qué no,
cuánto dinero generaron sus inversiones en marketing, y qué hacer la próxima semana.
Un cliente debe poder leer este reporte en 5 minutos y tomar decisiones.

## Process

### Phase 1 — Data Consolidation
Consolida todas las fuentes disponibles:
- Google Ads: clics, conversiones, gasto, CPC, CPA, ROAS
- Meta Ads: alcance, CPM, CTR, leads, CPL, ROAS
- LinkedIn Ads: impresiones, clics, leads, CPL, lead quality score
- SEO orgánico: clics GSC, posición media, páginas indexadas, tráfico GA

Si falta data de algún canal, marcarlo como “Sin datos — pendiente conexión” y continuar con los disponibles.

### Phase 2 — Anomaly Detection
Compara vs período anterior y detecta:

| Umbral | Clasificación |
|---|---|
| Métrica clave cayó >20% | 🔴 CRITICAL |
| Métrica clave cayó 10-20% | 🟡 WARN |
| Métrica clave mejoró >10% | 🟢 WIN |
| Variación <10% | ➡️ ESTABLE |

Métricas clave por canal:
- Google: conversiones, CPA
- Meta: CPL, leads
- LinkedIn: leads calificados, CPL
- SEO: clics orgánicos, posición media keywords target

### Phase 3 — Attribution & ROI Calculation
Si hay datos de revenue:
- ROAS total = Revenue atribuible / Inversión total en ads
- CAC = Gasto total / Nuevos clientes adquiridos
- LTV:CAC ratio = LTV estimado / CAC (meta: ≥ 3:1)

Si no hay revenue: reportar leads como proxy y calcular lead-to-close rate estimada.

### Phase 4 — Channel Ranking
Rankea canales por eficiencia (CPL o CPA):

| Posición | Canal | CPL/CPA | Calidad Lead | Recomendación |
|---:|---|---:|---|---|
| 1 | Google Ads | $X | Alta | Escalar budget |
| 2 | Meta Ads | $X | Media | Mantener + optimizar |
| 3 | LinkedIn | $X | Alta | Optimizar audiencia |
| — | SEO Orgánico | $0 | Alta | Invertir en contenido |

### Phase 5 — Recommendations Engine
Genera recomendaciones priorizadas por impacto potencial.

Score de prioridad:
- Impacto en resultado (leads/ventas): 1-10
- Urgencia (¿penaliza no actuar?): 1-10
- Esfuerzo de implementación (10=fácil): 1-10

Prioridad = (Impacto×0.4 + Urgencia×0.35 + Facilidad×0.25)

Top 5 recomendaciones con score ≥ 6 van en el reporte.

## Output Format

# Reporte de Marketing — [Client Name]

**Período:** [fecha inicio] - [fecha fin] | **Generado:** YYYY-MM-DD
**Canales activos:** [Google Ads] [Meta Ads] [LinkedIn] [SEO Orgánico]

---
## 📊 Resumen Ejecutivo (léelo en 2 minutos)

**Esta semana/mes:** [1-2 oraciones sobre qué pasó en términos de negocio — no métricas, sino resultado: "Generamos X leads a un costo promedio de $Y, el canal más eficiente fue Z"]

**El número más importante:** [una sola métrica clave con contexto]

**Decisión urgente requerida:** [si existe — ej. "El presupuesto de Google se agota 5 días antes del fin del mes, necesitamos decidir si aumentamos o pausamos"]

---
## 💰 Inversión y Resultados

| Canal | Inversión | Leads/Conv | CPL/CPA | vs Anterior | Estado |
|---|---:|---:|---:|---:|---|
| Google Ads | $X | X | $X | ↑/↓X% | 🔴🟡🟢 |
| Meta Ads | $X | X | $X | ↑/↓X% | 🔴🟡🟢 |
| LinkedIn Ads | $X | X | $X | ↑/↓X% | 🔴🟡🟢 |
| SEO Orgánico | $0 | X clics | $0 | ↑/↓X% | 🔴🟡🟢 |
| **TOTAL** | **$X** | **X** | **$X** | **↑/↓X%** | |

**ROAS total:** Xх (por cada $1 invertido, se generaron $X en valor estimado)

---
## 🚨 Alertas
- 🔴 **CRITICAL:** [qué pasó + impacto + acción requerida HOY]
- 🟡 **WARN:** [qué hay que monitorear]
- 🟢 **WIN:** [qué está funcionando bien — celebrar y escalar]

---
## 🏆 Qué Está Funcionando
- **Mejor campaña:** [nombre] — [métrica clave] — [por qué está funcionando]
- **Mejor anuncio:** [nombre] — CTR X% — [ángulo que resuena]
- **Mejor audiencia:** [descripción] — CPL $X — [insight]

---
## 📉 Qué Necesita Atención
- **Campaña más débil:** [nombre] — [problema específico]
- **Anuncio con mayor fatiga:** [nombre] — Frecuencia X.X — [acción]
- **Keyword más cara sin conversiones:** [keyword] — $X gastados, 0 conv

---
## 🔍 SEO Orgánico

| Métrica | Este período | Anterior | Δ |
|---|---:|---:|---:|
| Clics orgánicos | X | X | X% |
| Posición media | X | X | X pos |
| Impresiones | X | X | X% |
| Páginas indexadas | X | X | +/-X |

**Keyword estrella:** [keyword] — Posición X (subió/bajó X posiciones)

**Oportunidad AEO detectada:** [si hay queries donde aparecer en AI search]

---
## ✅ Plan de Acción — Próxima Semana

| # | Acción | Canal | Responsable | Impacto esperado | Fecha |
|---:|---|---|---|---|---|
| 1 | [acción específica] | [canal] | [owner] | [resultado esperado] | [fecha] |
| 2 |  |  |  |  |  |
| 3 |  |  |  |  |  |
| 4 |  |  |  |  |  |
| 5 |  |  |  |  |  |

---
## 📅 Próximo Reporte
[fecha del próximo reporte] | Foco: [qué vamos a medir especialmente]

## Anti-Patterns
- ❌ No reportar métricas de vanidad sin contexto — “tuvimos 50k impresiones” sin CPM, CTR o conversiones no ayuda a tomar decisiones.
- ❌ No comparar períodos con distinto número de días — semana con festivo vs semana normal distorsiona el análisis.
- ❌ No omitir anomalías porque “puede ser ruido” — si la caída supera el umbral, reportar siempre.
- ❌ No hacer reporte sin plan de acción — el cliente paga por decisiones, no por datos.
- ❌ No ignorar SEO orgánico — es el canal de mayor ROI a largo plazo y debe incluirse siempre.
- ❌ No usar jerga técnica en el resumen ejecutivo — el cliente final puede no ser marketer.

## Done Criteria
- [ ] Resumen ejecutivo legible en <2 minutos sin conocimiento técnico.
- [ ] Todos los canales activos reportados (o marcados como “sin datos”).
- [ ] Alertas clasificadas con umbral justificado.
- [ ] Al menos 1 WIN identificado (para balance y motivación del cliente).
- [ ] Plan de acción con mínimo 3 items, owner y fecha.
- [ ] ROAS total calculado o razón por la que no se puede calcular.
- [ ] Reporte enviable al cliente sin edición adicional.
