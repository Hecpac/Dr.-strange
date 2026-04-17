---
name: pending-items
description: >
  Rastrea y presenta todos los items abiertos en los canales de Hector y la actividad de agentes.
  Usa este skill cuando el agente asistente necesite listar pendientes, revisar compromisos abiertos,
  mostrar items sin resolver, o responder a "qué tengo pendiente", "qué me falta", "mis pendientes",
  "cosas abiertas", "qué estoy dejando pasar", "compromisos", "seguimiento", o cualquier solicitud
  de visibilidad sobre tareas o promesas sin cumplir.
  También aplica cuando alguien pregunta por items bloqueados esperando la decision de Hector.
---

# Pending Items

Rastrea todas las fuentes de Hector y consolida una lista unica de pendientes priorizados, sin duplicados, con contexto accionable.

## Trigger

Diario despues de que daily-brief termine, o bajo demanda manual.

## Inputs requeridos

| Input                              | Fuente                                                    | Requerido      |
|-----------------------------------|-----------------------------------------------------------|----------------|
| Mensajes recientes de Telegram    | Hilos no resueltos de las ultimas 48h                      | Si             |
| Mensajes del bus                  | Inbox de Alma y broadcasts de las ultimas 48h              | Si             |
| Recordatorios activos             | Memoria de Alma                                            | Si             |
| Calendario                        | Eventos con action items                                   | Recomendado   |
| Outputs de otros agentes          | Resultados de Hex, Rook, Lux que mencionen a Hector o requieran su input | Recomendado   |

Si no hay acceso directo a alguna fuente, indicar que esa fuente no fue consultada en la salida.

## Proceso

### 1. Escaneo de fuentes
Revisa todas las fuentes buscando items que cumplan al menos uno de estos criterios:
- Contienen una pregunta dirigida a Hector
- Mencionan un deadline
- Fueron prometidos por Hector a alguien
- Estan bloqueados esperando una decision de Hector

Para cada item encontrado, registra: descripcion breve, fuente (ID de mensaje, evento de calendario, o mensaje del bus), quien lo pidio, cuando se origino.

### 2. Deduplicacion
Si el mismo item aparece en multiples fuentes (ej: mencionado en Telegram y tambien como mensaje del bus), fusionarlos en uno solo.
- Conservar la fuente mas reciente como referencia principal.
- Anotar las fuentes adicionales como "tambien visto en: {fuente}".

### 3. Enriquecimiento con contexto
Para cada item unico, agregar:
- Quien lo pidio y cuando
- En que conversacion o contexto surgio
- Cual es la consecuencia de no actuar (si es identificable)

### 4. Priorizacion por urgencia

| Prioridad        | Criterio                                                         |
|-----------------|------------------------------------------------------------------|
| **Urgente**      | Deadline hoy o vencido. Requiere accion inmediata.              |
| **Esta semana**  | Deadline dentro de los proximos 7 dias.                          |
| **Sin deadline** | No tiene fecha limite explicita. Nice-to-have o seguimiento.    |

Dentro de cada nivel, ordenar por impacto: items que bloquean a otras personas primero, luego items que bloquean trabajo propio.

## Output format

## Pendientes -- {fecha}

### Urgente (hoy/vencido)
1. {item} -- {quien lo pidio, cuando} -- {accion sugerida}

### Esta semana
1. {item} -- {contexto} -- {deadline}

### Sin deadline
1. {item} -- {contexto}

**Total:** {count} items ({urgent} urgentes)

## Done criteria
- [ ] 0 items duplicados en la lista final.
- [ ] Cada item tiene una fuente verificable (ID de mensaje de Telegram, evento de calendario, ID de mensaje del bus).
- [ ] Todos los items urgentes incluyen una accion sugerida especifica.
- [ ] La salida esta completamente en espanol.
- [ ] Si una fuente no pudo ser consultada, se indica explicitamente al final.

## Errores comunes a evitar
- No inventes items: Si no hay pendientes, di que no hay. Una lista vacia es una buena noticia.
- No seas vago en la accion sugerida: "Revisar el tema" no sirve.
  "Responder a Luis en el hilo de Telegram confirmando fecha de entrega" si sirve.
- No mezcles prioridades: Un item sin deadline no es urgente solo porque suena importante.
  Urgente = tiene deadline hoy o ya vencio. Punto.
- No ignores el contexto: Decir "pendiente con cliente" sin nombre, fecha ni consecuencia no ayuda a Hector a actuar.
