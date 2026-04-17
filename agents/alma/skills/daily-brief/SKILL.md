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

Genera un brief ejecutivo diario que en < 2 minutos de lectura te deja listo para actuar.

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

### 2. Priorización (Regla 3-1-1)
De todo lo recopilado, selecciona:
- 3 prioridades del día: Las 3 cosas que, si se completan, hacen que el día sea productivo.
- 1 bloqueo principal: El obstáculo más grande que puede frenar el progreso.
- 1 decisión pendiente: La decisión que, si no se toma, bloquea otras cosas.

Criterios para seleccionar las 3 prioridades:

| Prioridad | Criterio |
|----------|----------|
| **#1**   | Lo más urgente + importante. Deadline hoy o impacto directo en revenue/cliente. |
| **#2**   | Lo que desbloquea otras cosas. Si esto se hace, otras tareas avanzan. |
| **#3**   | Lo que más avanza objetivos de la semana/mes, aunque no sea urgente hoy. |

Cada prioridad debe ser accionable en el día (no “avanzar con el proyecto” sino “terminar la sección X del documento Y”).

### 3. Quick-start action
Define UNA acción que se pueda ejecutar en < 5 minutos y que genere momentum para el día.
Esto es lo primero que el usuario debería hacer al leer el brief.

Ejemplos buenos:
- “Responder el email de [cliente] confirmando la reunión del jueves”
- “Hacer push del fix en `api/auth.ts`”
- “Revisar y aprobar el PR #47”

Ejemplos malos:
- “Revisar emails”
- “Trabajar en el proyecto”
- “Pensar en la estrategia”

## Output format

## 📋 Daily Brief — [día, fecha]

### Estado general: [🟢 Todo OK / 🟡 Hay alertas / 🔴 Hay problemas]
[1 línea de contexto operativo. Ej: "Servicios estables. Newsletter enviada ayer con 42% open rate."]

---
### 🎯 Top 3 prioridades
1. **[Prioridad #1]** — [por qué hoy]
   → Resultado esperado: [qué se ve diferente al final del día si esto se completa]
2. **[Prioridad #2]** — [qué desbloquea]
   → Resultado esperado: [outcome concreto]
3. **[Prioridad #3]** — [conexión con objetivo mayor]
   → Resultado esperado: [outcome concreto]

---
### 🚧 Bloqueo principal
**[Descripción del bloqueo]**
→ Requiere: [qué necesitas para desbloquearlo: respuesta de alguien, decisión, recurso]
→ Si no se resuelve hoy: [consecuencia concreta]

### ❓ Decisión pendiente
**[Descripción de la decisión]**
→ Opciones: [A] vs [B]
→ Deadline para decidir: [cuándo]

---
### ⚡ Quick-start (< 5 min)
**[Acción concreta e inmediata]**

## Variantes

### Brief de mitad de día
Si se solicita un check-in de mitad de día:
- Revisa avance de las 3 prioridades del brief matutino (✅ hecho / 🔄 en progreso / ❌ bloqueado).
- Ajusta prioridades si algo cambió.
- Redefine quick-start para la tarde.

### Brief de cierre
Si se solicita al final del día:
- Lista qué se completó vs qué se planeó.
- Identifica carry-overs para mañana.
- Nota aprendizajes o cambios de contexto relevantes.

## Done criteria
- [ ] El brief completo se lee en < 2 minutos (aprox. 200-300 palabras).
- [ ] Las 3 prioridades son accionables hoy (no genéricas ni multi-día).
- [ ] Cada prioridad tiene resultado esperado concreto.
- [ ] Hay exactamente 1 quick-start action ejecutable en < 5 minutos.
- [ ] Si hay bloqueos, incluyen qué se necesita para desbloquear y consecuencia de no hacerlo.
- [ ] El estado general es claro al primer vistazo (emoji + 1 línea).

## Errores comunes a evitar
- No metas más de 3 prioridades: Si todo es prioridad, nada lo es. El límite de 3 es inviolable.
- No seas vago: “Trabajar en el proyecto X” no es una prioridad.
  “Completar la migración del endpoint /users a la nueva API” sí lo es.
- No ignores carry-overs: Si algo se pospuso ayer, debe aparecer hoy con mayor urgencia o con una decisión explícita de descartarlo.
- No hagas el brief largo: Si pasa de 300 palabras, estás fallando. Corta sin piedad.
- No olvides el quick-start: El cerebro necesita una victoria temprana. Siempre incluye algo que se pueda hacer YA.
