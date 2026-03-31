---
name: content-radar
description: >
  Detecta 3 oportunidades de contenido de alto impacto basadas en señales actuales.
  Usa este skill cuando el agente de marketing necesite ideas de contenido,
  detectar tendencias, planificar publicaciones, o responder a “qué publicar”,
  “oportunidades de contenido”, “temas trending”, “calendario editorial”,
  o “qué contenido crear”. También aplica para análisis de gaps de contenido,
  reacción a noticias del nicho, o cuando se necesite contenido alineado con SEO/AEO.
---

# Content Radar

Escanea señales del mercado y entrega 3 oportunidades de contenido listas para ejecutar, con ángulo diferenciador y plan de acción.

## Inputs requeridos

| Input                  | Fuente                                                       | Requerido                                  |
|-----------------------|--------------------------------------------------------------|--------------------------------------------|
| Nicho/industria objetivo | Definición del negocio del cliente                         | ✅ Sí                                      |
| Señales recientes        | Noticias, trends en X/LinkedIn, cambios de algoritmo, launches | ✅ Sí (investigar si no se proveen)     |
| Activos existentes       | Blog posts, casos de estudio, redes sociales, landing pages | Recomendado                                |
| Audiencia objetivo       | Perfil del cliente ideal, pain points conocidos             | Recomendado                                |
| Restricciones            | Formatos disponibles, capacidad de producción, deadline real | Si disponible                             |

## Proceso

### 1. Escaneo de señales (Signal Detection)
Busca en estas 4 capas:

| Capa                 | Qué buscar                                        | Ejemplo                                   |
|----------------------|---------------------------------------------------|-------------------------------------------|
| **Tendencia macro**  | Cambios en la industria, regulaciones, tecnología | “Google actualiza E-E-A-T guidelines”     |
| **Conversación activa** | Lo que la audiencia está discutiendo ahora      | “Thread viral sobre AI replacing agencies” |
| **Gap de competencia** | Temas que nadie está cubriendo bien             | “Nadie explica AEO para servicios locales” |
| **Evento/timing**    | Fechas, lanzamientos, conferencias próximas       | “Google I/O la próxima semana”            |

Registra al menos 6-8 señales antes de filtrar.

### 2. Filtro de potencial
Para cada señal, evalúa:

| Criterio            | Pregunta clave                                                  | Peso |
|---------------------|------------------------------------------------------------------|------|
| **Relevancia**      | ¿Conecta directamente con lo que vende/hace el negocio?         | 3x   |
| **Timing**          | ¿Hay una ventana de oportunidad? ¿Es ahora o nunca?             | 2x   |
| **Diferenciación**  | ¿Podemos decir algo que otros no están diciendo?                | 2x   |
| **Esfuerzo**        | ¿Se puede producir esta semana con recursos actuales?           | 1x   |

Descarta señales con relevancia < 3/5.
Selecciona las top 3.

### 3. Desarrollo de cada oportunidad
Para cada una de las 3 ideas seleccionadas, define:
- Ángulo diferenciador:
  - No el tema genérico, sino TU perspectiva única.
  - “AI en marketing” es un tema.
  - “Por qué las agencias que no ofrecen AEO van a perder el 40% de su tráfico en 2026” es un ángulo.
- Formato óptimo: Elige basándote en dónde vive la audiencia y qué formato amplifica mejor el mensaje.
- Hook: La primera línea que captura atención. Debe funcionar en el feed.
- CTA: Qué acción queremos que tome el lector. Debe ser específica y medible.
- Conexión con negocio: Cómo este contenido mueve métricas del negocio (leads, autoridad, SEO).

## Output format

## 📡 Content Radar — [fecha]

**Nicho**: [industria/negocio]
**Señales escaneadas**: [N señales → 3 seleccionadas]

---
### 🎯 Oportunidad 1: [Título del ángulo]
**Señal detectada**: [Qué está pasando en el mercado]
**Por qué ahora**: [Ventana de timing + por qué importa]
**Ángulo**: [Tu perspectiva diferenciadora — 1-2 oraciones]
**Formato**: [Blog post / Video corto / Thread / Newsletter / Caso de estudio]
**Canal principal**: [Dónde publicar primero]
**Hook**: "[Primera línea exacta]"
**CTA**: [Acción específica: agendar call, descargar guía, comentar, etc.]
**Conexión negocio**: [Cómo esto genera leads/autoridad/tráfico]
**Deadline sugerido**: [Fecha concreta]
**Esfuerzo estimado**: [Horas aproximadas de producción]

---
[Repetir para Oportunidades 2 y 3]

### Señales descartadas (para referencia futura)
- [Señal X]: Descartada por [razón breve]

## Done criteria
- [ ] Hay exactamente 3 oportunidades (no 2, no 5).
- [ ] Cada oportunidad tiene un ángulo diferenciador, no solo un tema genérico.
- [ ] Cada hook funciona como primera línea de un post real (testeable: ¿harías scroll stop?).
- [ ] Cada CTA es específica y medible (no “visita nuestro sitio”).
- [ ] Al menos 1 oportunidad es ejecutable en las próximas 48 horas.
- [ ] Las oportunidades están conectadas con objetivos de negocio, no solo engagement.

## Errores comunes a evitar
- No seas genérico: “Escribir sobre IA” no es una oportunidad.
  “Tutorial: cómo configurar AEO para tu sitio de servicios en 30 minutos” sí lo es.
- No ignores el timing: Un tema evergreen no es content radar. Radar implica una señal ACTUAL que requiere acción AHORA.
- No olvides la conexión con negocio: Contenido viral que no genera leads es vanity metric.
  Cada pieza debe tener un camino claro hacia conversión.
- No propongas lo que no se puede producir: Si el equipo es de 1 persona, no propongas un mini-documental.
