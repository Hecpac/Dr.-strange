---
name: context-bridge
description: >
  Traduce entre el contexto personal de Hector y los dominios tecnicos de otros agentes.
  Usa este skill cuando Hector mencione algo tecnico en Telegram que deba enrutarse a otro agente,
  cuando un agente solicite contexto personal via bus para tomar una decision, o cuando se necesite
  traducir entre el lenguaje de Hector y el dominio de Hex, Rook, o Lux.
  Aplica cuando se detecte "pasale esto a Hex", "dile a Rook que...", "Lux necesita saber que...",
  o cualquier mensaje que implique comunicacion entre Hector y un agente especifico,
  asi como cuando un agente envie un request via bus pidiendo contexto adicional.
---

# Context Bridge

Traduce y enriquece mensajes entre el contexto personal de Hector y los dominios especializados de otros agentes, asegurando que el agente destino pueda actuar sin pedir mas informacion y sin recibir datos personales innecesarios.

## Trigger

- Hector menciona algo tecnico en Telegram que deberia enrutarse a otro agente.
- Un agente envia un mensaje via bus solicitando contexto personal para tomar una decision.

## Inputs requeridos

| Input                              | Fuente                                                    | Requerido      |
|-----------------------------------|-----------------------------------------------------------|----------------|
| Mensaje o request fuente          | Mensaje de Telegram o mensaje del bus                      | Si             |
| Contexto reciente de Hector       | Calendario, conversaciones recientes, prioridades declaradas | Si             |
| Estado del agente destino         | Dominio, capacidades, estado actual (desde registry)       | Recomendado   |
| Energia/animo de Hector           | Solo si fue mencionado explicitamente en conversacion      | Si disponible |

## Proceso

### 1. Deteccion de intent
Determinar que necesita realmente el mensaje fuente:
- **Informacion:** El agente destino necesita saber algo para hacer su trabajo.
- **Accion:** Se requiere que el agente destino ejecute algo.
- **Decision:** Hay una decision pendiente que requiere contexto cruzado.
- **Contexto:** El agente destino pidio contexto adicional para continuar.

### 2. Ensamblaje de contexto
Recopilar el contexto personal relevante SIN sobre-compartir:
- Calendario: solo eventos relacionados con el tema.
- Conversaciones: solo fragmentos relevantes al request.
- Prioridades: solo las que afectan la urgencia o direccion del request.
- NO incluir: datos de salud, relaciones, finanzas, o cualquier dato personal que no sea directamente relevante para la tarea.

### 3. Traduccion por tipo de agente
Reformular el mensaje en los terminos que el agente destino entiende:

| Agente destino | Framing                                                        |
|---------------|----------------------------------------------------------------|
| **Hex**        | Tecnico: referencias a repo/archivo, nivel de prioridad, scope del cambio |
| **Rook**       | Operacional: urgencia, servicios afectados, impacto en disponibilidad     |
| **Lux**        | Negocio: audiencia, timeline, voz de marca, objetivo de negocio           |

Ejemplo:
- Hector dice en Telegram: "El sitio de Pachano esta lento, creo que es por las imagenes del portfolio nuevo que subio ayer"
- Para Hex: `{topic: "performance_issue", payload: {repo: "pachanodesign", area: "frontend/images", context: "imagenes de portfolio subidas ayer causan lentitud", priority: "normal"}}`
- Para Rook: `{topic: "performance_degradation", payload: {service: "pachanodesign.com", symptom: "carga lenta", probable_cause: "imagenes pesadas en portfolio", since: "ayer"}}`

### 4. Filtro de privacidad
Antes de enviar, revisar el payload completo y eliminar:
- Datos personales no relevantes (salud, relaciones, finanzas)
- Opiniones personales de Hector sobre personas
- Informacion confidencial de clientes no relacionada con la tarea

**Regla critica:** Si hay duda sobre si un dato es necesario o no, NO enviarlo y preguntar a Hector antes.

### 5. Entrega via bus
Enviar como `AgentMessage` al agente destino:

```
AgentMessage(
    from_agent: "alma",
    to_agent: "{agente_destino}",
    intent: "notify" | "request",    # notify si es informacion, request si se necesita accion
    topic: "context_bridge",
    payload: {
        original_request: "{mensaje original de Hector o request del agente}",
        enriched_context: "{contexto relevante ensamblado}",
        suggested_action: "{que deberia hacer el agente destino}",
        privacy_note: "{que datos se omitieron y por que, si aplica}"
    },
    priority: "normal" | "urgent"    # urgent solo si Hector lo indico o el deadline es hoy
)
```

## Output format

El output principal es el `AgentMessage` enviado via bus. Adicionalmente, Alma confirma a Hector:

> Listo, le pase a {agente} el contexto sobre {tema}. Le dije que {resumen de suggested_action}. {privacy_note si se omitio algo}.

## Done criteria
- [ ] El agente destino puede actuar sin necesidad de pedir mas contexto a Hector.
- [ ] No se filtraron datos personales innecesarios (salud, relaciones, finanzas no relevantes).
- [ ] El intent original de Hector se preservo fielmente (no fue reinterpretado).
- [ ] Si Alma no esta segura sobre la privacidad de un dato, le pregunta a Hector antes de enviar.
- [ ] El mensaje esta formateado como AgentMessage valido con todos los campos requeridos.
- [ ] La confirmacion a Hector esta en espanol.

## Errores comunes a evitar
- No reinterpretes lo que Hector quiere: Si dice "dile a Hex que revise el PR", no lo conviertas en "Hex deberia hacer code review completo con analisis de seguridad". Preserva el intent original.
- No sobre-compartas: Que Hector haya mencionado que esta cansado no es relevante para un bug report a Hex. Filtralo.
- No sub-compartas: Si Hector menciono que el cliente necesita el fix para el jueves, esa fecha es critica para el agente destino. No la omitas.
- No envies sin contexto suficiente: Si el mensaje de Hector es ambiguo ("arregla lo del sitio"), pide clarificacion antes de enviar un context bridge incompleto.
- No asumas urgencia: Solo marca como urgent si Hector lo dijo explicitamente o hay un deadline inmediato. "Normal" es el default.
