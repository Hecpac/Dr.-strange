---
name: bug-triage
description: >
  Prioriza bugs y devuelve top 5 fixes accionables con impacto estimado.
  Usa este skill cuando el agente de desarrollo necesite revisar bugs pendientes,
  priorizar issues, hacer triage de errores, o decidir qué arreglar primero.
  También aplica cuando se mencionen errores recurrentes, crashes, regresiones,
  o cualquier solicitud de “qué bugs atacar”, “estado del backlog”, o “qué está roto”.
---

# Bug Triage

Analiza bugs abiertos/recurrentes y entrega un plan de acción priorizado con 5 fixes concretos.

## Inputs requeridos

Antes de ejecutar, recopila esta información:

| Input                        | Fuente                                           | Requerido      |
|-----------------------------|--------------------------------------------------|----------------|
| Lista de issues/bugs abiertos | GitHub Issues, Linear, logs, o reporte del usuario | ✅ Sí         |
| Errores recientes en logs/CI  | Logs de producción, CI/CD, Sentry, o equivalente   | Recomendado   |
| Contexto del proyecto         | README, stack técnico, servicios activos            | Recomendado   |
| Impacto en usuarios           | Métricas, quejas, tickets de soporte                | Si disponible |

Si no tienes acceso directo a las fuentes, pide al usuario que pegue o describa la información relevante.

## Proceso

### 1. Inventario
- Lista todos los bugs identificados (abiertos + recurrentes en logs).
- Para cada uno registra: descripción breve, frecuencia, componente afectado, fecha de primera aparición.
- Si un bug aparece más de 3 veces en los últimos 7 días, márcalo como recurrente.

### 2. Scoring de impacto
Evalúa cada bug en 3 dimensiones (1-5):

| Dimensión         | 1 (Bajo)       | 3 (Medio)               | 5 (Alto)                               |
|------------------|----------------|--------------------------|----------------------------------------|
| **Usuario**       | Edge case raro | Funcionalidad degradada  | Bloquea flujo crítico                  |
| **Negocio**       | Sin impacto en revenue | Afecta conversión menor | Pérdida directa de clientes/ingresos |
| **Riesgo técnico**| Aislado        | Puede escalar            | Deuda técnica cascada / seguridad      |

Score total = (Usuario × 2) + Negocio + Riesgo técnico.
Máximo posible: 20.

### 3. Clasificación de prioridad

| Prioridad          | Score | SLA sugerido |
|-------------------|-------|--------------|
| **P0 - Critical** | 15-20 | Arreglar hoy |
| **P1 - High**     | 10-14 | Esta semana  |
| **P2 - Medium**   | 5-9   | Este sprint  |
| **P3 - Low**      | 1-4   | Backlog      |

### 4. Fix mínimo viable
Para cada bug del top 5, define:
- Qué cambiar: archivo/componente/endpoint específico.
- Cómo: descripción técnica concreta (no vaga).
  - Ejemplo: “Agregar null check en processOrder() línea 47”
  - NO: “revisar el manejo de errores”.
- Estimación: T-shirt size (XS: <30min, S: <2h, M: <1 día, L: >1 día).
- Riesgo del fix: ¿puede romper algo más? Si sí, qué tests correr.

## Output format

## 🔧 Bug Triage Report — [fecha]

**Resumen**: X bugs analizados | P0: N | P1: N | P2: N

### Top 5 Fixes

#### 1. [Nombre del bug] — P0 (Score: 18/20)
- **Descripción**: [qué pasa]
- **Impacto**: Usuario: 5 | Negocio: 4 | Riesgo: 4
- **Frecuencia**: [X ocurrencias en Y período]
- **Fix propuesto**: [cambio específico]
- **Estimación**: [T-shirt size]
- **Riesgo del fix**: [bajo/medio/alto + por qué]
- **⏭️ Siguiente paso**: [acción concreta en <5 min para empezar]

[Repetir para items 2-5]

### Bugs descartados del top (si hay)
- [Bug X]: Score 4/20 — razón breve de por qué no entra.

## Done criteria
- [ ] Hay exactamente 5 items priorizados (o menos si no hay 5 bugs).
- [ ] Cada item tiene score numérico y prioridad P0-P3.
- [ ] Cada fix es técnicamente específico (archivo, función, o componente nombrado).
- [ ] Cada item tiene un “siguiente paso” ejecutable en menos de 5 minutos.
- [ ] Los scores son consistentes entre sí (el #1 tiene mayor score que el #5).

## Errores comunes a evitar
- No seas vago: “Mejorar error handling” no es un fix.
  “Agregar try/catch en api/orders.ts:processPayment() con fallback a estado pending” sí lo es.
- No ignores recurrencia: Un bug “menor” que ocurre 50 veces/día puede ser peor que un P0 que pasó una vez.
- No asumas contexto: Si no tienes info suficiente para scorear, pregunta antes de inventar números.
