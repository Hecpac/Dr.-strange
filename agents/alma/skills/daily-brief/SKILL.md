---
name: daily-brief
description: >
  Entrega un brief diario ejecutivo en formato corto orientado a ejecución.
  Usa este skill cuando el agente asistente necesite preparar el resumen del día,
  dar contexto matutino, o responder a “qué hay para hoy”, “brief del día”,
  “resumen diario”, “prioridades de hoy”, “daily standup”, “qué debo hacer hoy”,
  o cualquier solicitud de orientación ejecutiva para comenzar o revisar el día.
  También aplica para check-ins de mitad de día o cierre diario.
---

# Daily Brief

Genera una apertura o cierre operativo en lenguaje natural, con evidencia real y
sin formato de boletín.

## Inputs requeridos

| Input                     | Fuente                                                  | Requerido      |
|--------------------------|---------------------------------------------------------|----------------|
| Estado operativo actual  | Health audit, status de servicios, métricas clave      | ✅ Sí         |
| Tareas/compromisos del día | Calendario, to-do list, sprints activos, deadlines    | ✅ Sí         |
| Bloqueos conocidos       | Dependencias pendientes, decisiones sin tomar, esperas  | Recomendado   |
| Contexto de ayer         | Brief anterior, tareas completadas/pospuestas           | Recomendado   |
| Pipeline de negocio      | Propuestas pendientes, leads activos, entregas próximas | Si disponible |

## Proceso

### 1. Captura de contexto
Recopila el estado actual revisando (en orden):
1. Operativo: ¿Hay algo roto o en riesgo? (Si hay health-audit reciente, úsalo)
2. Compromisos: ¿Qué está prometido para hoy/esta semana?
3. Pipeline: ¿Hay propuestas, entregas, o deadlines próximos?
4. Carry-over: ¿Qué quedó pendiente de ayer?

### 2. Síntesis operacional
Convierte los hechos en 2-4 párrafos cortos. Debe sentirse como continuidad de
un agente que estuvo observando, no como una plantilla.

Selecciona solo lo que tenga evidencia:
- Qué cambió desde el último corte.
- Qué sigue abierto.
- Qué está bloqueado o esperando decisión.
- Qué movimiento concreto conviene hacer ahora o mañana.

### 3. Apertura y cierre
La primera frase debe situar el momento exacto:
- Apertura: "Arranque del lunes 27 de abril de 2026..."
- Cierre: "Corte del lunes 27 de abril de 2026..."

La última frase debe nombrar el siguiente movimiento si hay evidencia. Si no hay
señal, dilo sin rellenar.

Ejemplos buenos:
- “Responder el email de [cliente] confirmando la reunión del jueves”
- “Hacer push del fix en `api/auth.ts`”
- “Revisar y aprobar el PR #47”

Ejemplos malos:
- “Revisar emails”
- “Trabajar en el proyecto”
- “Pensar en la estrategia”

## Output contract

La salida pública es texto plano conversacional:

Arranque del lunes 27 de abril de 2026. Tengo agenda 2 eventos hoy y correo 3
correos importantes.

La bitácora cubre la continuación de ayer. Para retomar hoy queda revisar el
router de Telegram y cerrar la verificación en observe_stream.

El siguiente movimiento es validar el evento agent_startup_context antes de
decir que el boot quedó resuelto.

No uses Markdown, bullets, headers, emojis, tablas, negritas, task IDs crudos,
approval IDs ni lenguaje de reporte.

## Variantes

### Brief de mitad de día
Si se solicita un check-in de mitad de día:
- Revisa avance de lo que quedó abierto en la apertura.
- Ajusta prioridades si algo cambió.
- Redefine el siguiente movimiento para la tarde.

### Brief de cierre
Si se solicita al final del día:
- Lista qué se completó vs qué se planeó.
- Identifica carry-overs para mañana.
- Nota aprendizajes o cambios de contexto relevantes.

## Done criteria
- [ ] El brief completo se lee en < 2 minutos.
- [ ] La primera frase sitúa fecha y tipo de corte.
- [ ] Cada pendiente viene de una fuente verificable.
- [ ] El siguiente movimiento es concreto.
- [ ] Si hay bloqueos, incluyen qué se necesita para desbloquear y consecuencia de no hacerlo.
- [ ] La salida no contiene Markdown ni IDs internos.

## Errores comunes a evitar
- No seas vago: “Trabajar en el proyecto X” no es una prioridad.
  “Completar la migración del endpoint /users a la nueva API” sí lo es.
- No ignores carry-overs: Si algo se pospuso ayer, debe aparecer hoy con mayor urgencia o con una decisión explícita de descartarlo.
- No hagas el brief largo: Si pasa de 300 palabras, estás fallando. Corta sin piedad.
- No inventes energía o drama. Si no hay señal, dilo seco y propone verificar fuentes.
