---
name: weekly-retro
description: >
  Genera una retrospectiva semanal basada en datos reales del ecosistema de agentes.
  Usa este skill cuando el agente asistente necesite hacer la retro de la semana,
  revisar logros, detectar patrones, o responder a "retro semanal", "como fue la semana",
  "resumen de la semana", "que se logro", "que quedo pendiente", "revision semanal",
  "weekly review", o cualquier solicitud de analisis retrospectivo del trabajo semanal.
  Se ejecuta automaticamente los domingos a las 10:00 AM CT.
---

# Weekly Retro

Genera una retrospectiva semanal concisa (<300 palabras) basada exclusivamente en datos reales: git, bus, registry, y outputs de agentes.

## Trigger

Cron: Domingos a las 10:00 AM CT. Tambien puede ejecutarse manualmente.

## Inputs requeridos

| Input                          | Fuente                                                  | Requerido      |
|-------------------------------|--------------------------------------------------------|----------------|
| Git log de la semana          | Todos los repos, commits y PRs mergeados               | Si             |
| Historial del bus             | Mensajes enviados/recibidos durante la semana           | Si             |
| Snapshots del registry        | Estados diarios de agentes (AGENTS.md)                  | Si             |
| Outputs de Lux                | Contenido publicado, campanas ejecutadas                | Recomendado   |
| Outputs de Rook               | Reportes de incidentes, alertas resueltas               | Recomendado   |
| Daily briefs de Alma          | Briefs de la semana para rastrear carry-overs           | Recomendado   |

## Proceso

### 1. Logros (achievements)
Extraer logros concretos de fuentes reales:
- **Git:** PRs mergeados, features shipped (con SHA y fecha)
- **Lux:** Contenido publicado (titulo, fecha, plataforma)
- **Rook:** Incidentes detectados y resueltos (ID, severidad, tiempo de resolucion)
- **Alma:** Decisiones tomadas, bloqueos desbloqueados

Cada logro debe tener evidencia verificable. Si no hay SHA, fecha, o ID, no es un logro — es una suposicion.

### 2. Pendiente arrastrado (carryover)
Revisar la retro anterior (si existe) e identificar items marcados como "para la proxima semana" que siguen abiertos.
- Para cada uno: que lo bloqueo, sigue siendo relevante, o debe descartarse.

### 3. Deteccion de patrones
Buscar UN patron significativo en los datos de la semana:
- Bloqueos recurrentes (mismo tipo de problema aparecio >2 veces)
- Concentracion temporal (la mayoria de commits en un solo dia)
- Agente que necesito mas intervencion manual
- Tipo de tarea que consumio mas tiempo/costo

Maximo 1 patron. Elegir el mas significativo y respaldarlo con datos.

### 4. Insight
Una observacion no obvia que surja del cruce de datos:
- Ejemplo bueno: "El 80% de los PRs de la semana fueron los martes. Los miercoles tuvieron 0 commits."
- Ejemplo bueno: "El costo de Lux se disparo el jueves — correlaciona con la ejecucion de keyword research."
- Ejemplo malo: "Fue una semana productiva." (esto no dice nada)

### 5. Sugerencia
Una accion concreta y especifica para la proxima semana.
- Ejemplo bueno: "Mover el cron de SEO audit de lunes 8AM a lunes 10AM para evitar conflicto con health-audit."
- Ejemplo malo: "Ser mas productivo."

## Output format

## Retro Semanal -- {rango de la semana, ej: 25-31 Mar 2026}

### Logrado
- {logro con evidencia: SHA, fecha, ID, o referencia concreta}

### Pendiente arrastrado
- {item} -- {fecha original} -- {que lo bloquea}

### Patron detectado
{descripcion del patron con datos que lo respaldan}

### Sugerencia para esta semana
{sugerencia especifica y accionable}

## Done criteria
- [ ] Los logros provienen de datos reales (git SHA, fechas de publicacion, IDs de incidentes).
- [ ] Maximo 1 patron detectado (el mas significativo, no una lista).
- [ ] La sugerencia es especifica y accionable (no generica).
- [ ] El total es menor a 300 palabras.
- [ ] La salida esta completamente en espanol.
- [ ] Si no hay datos suficientes para una seccion, se indica explicitamente en lugar de inventar.

## Errores comunes a evitar
- No inventes logros: Si no hay SHA ni fecha, no lo listes. "Se avanzo en el proyecto" no es un logro verificable.
- No listes 5 patrones: Maximo 1. Si hay varios, elige el que tiene mas impacto y datos de respaldo.
- No hagas la retro larga: 300 palabras es el limite. Cada palabra debe aportar valor.
- No ignores el carryover: Si la retro anterior tenia pendientes, deben aparecer aqui — resueltos o arrastrados.
- No des sugerencias vagas: "Mejorar procesos" no le sirve a nadie. "Agregar un timeout de 30s al cron de backup para evitar que se encime con el health-audit" si sirve.
