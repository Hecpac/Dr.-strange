---
name: content-brief-generator
version: 2.0.0
category: marketing/content
description: >
  Genera briefs de contenido detallados y listos para redacción, optimizados
  simultáneamente para SEO (ranking en Google) y AEO (citabilidad por AI engines
  como ChatGPT, Perplexity, Claude). Convierte keywords y objetivos de negocio
  en especificaciones exactas para redactores o agentes de contenido.
triggers:
  - "crea un brief de contenido para"
  - "qué debería incluir el artículo sobre"
  - "brief para el blog de"
  - "necesito contenido sobre [tema] para"
  - "qué escribir para atacar la keyword"
  - "post-keyword-intelligence (siempre que hay Tier 1 informacionales)"
inputs:
  required:
    - keyword_primary: Keyword principal objetivo
    - client_name: Nombre del cliente
    - url_destino: URL donde publicar el contenido
  optional:
    - keyword_secondary: Keywords secundarias relacionadas
    - competitor_urls: URLs de artículos competidores a superar
    - tone: "profesional" | "conversacional" | "técnico" | "educativo"
    - content_type: "blog_post" | "landing_page" | "faq" | "case_study"
    - word_count_target: Extensión objetivo en palabras
---

## Objective

Producir un brief tan completo que un redactor (humano o IA) pueda crear el contenido final sin preguntas adicionales.
El contenido resultante debe ranquear en Google Y ser citado por AI engines.

## Process

### Phase 1 — SERP & Intent Analysis
1. Analiza los top 10 resultados para la keyword principal.
2. Identifica el tipo de contenido dominante: artículo, lista, guía, FAQ, video.
3. Detecta la intención real: informacional, comparativa, de procedimiento.
4. Mide extensión promedio de los top 5 resultados.
5. Identifica featured snippets existentes y su formato (párrafo, lista, tabla).

### Phase 2 — AEO Opportunity Mapping
1. Busca cómo responden ChatGPT/Perplexity/Google AI Overview la query principal.
2. Identifica qué fuentes citan y por qué (datos, autoridad, claridad).
3. Detecta preguntas relacionadas (PAA - People Also Ask).
4. Define el “snippet perfecto”: respuesta directa en 40-60 palabras que puede ser extraída por IA.

### Phase 3 — Content Architecture
Estructura el esquema H1-H2-H3 basado en:
- Intención principal del usuario
- Subtemas cubiertos por competidores (para paridad)
- Subtemas NO cubiertos por competidores (para diferenciación)
- Preguntas de PAA a responder

### Phase 4 — Schema Markup Recommendations
Según el tipo de contenido:
- FAQ: FAQ Schema para cada pregunta — aumenta probabilidad de featured snippet.
- HowTo: HowTo Schema con pasos numerados.
- Article: Article Schema con autor, fecha, imagen.
- Product/Service: Service Schema con descripción, precio, área de servicio.

## Output Format

# Content Brief — [Client Name]

**Keyword principal:** [keyword]
**URL destino:** [url]
**Fecha:** YYYY-MM-DD | **Tipo:** [blog_post/landing/faq]

---
## Contexto de Negocio

**Por qué crear este contenido:** [conexión entre la keyword y el objetivo de negocio del cliente]
**Conversión objetivo:** [qué debe hacer el lector al terminar — contactar, descargar, comprar]

---
## Keywords

| Tipo | Keyword | Volumen/mes | Ubicación en texto |
|---|---|---:|---|
| Principal | [keyword] | X | H1, párrafo intro, URL, meta title |
| Secundaria | [keyword] | X | H2, cuerpo |
| Secundaria | [keyword] | X | H2, cuerpo |
| LSI | [variante semántica] | — | Cuerpo, naturalmente |
| LSI | [variante semántica] | — | Cuerpo, naturalmente |
| Long-tail AEO | [pregunta conversacional] | — | FAQ / H3 |

---
## Meta Tags

**Meta Title:** [keyword principal] — [propuesta de valor] | [Marca] (55-60 chars)
`[título exacto aquí]`

**Meta Description:** [respuesta directa a la intención + CTA] (150-160 chars)
`[descripción exacta aquí]`

**URL slug:** /[slug-con-keyword-principal]

---
## Snippet Perfecto (AEO)

Este párrafo debe aparecer en las primeras 100 palabras del artículo para ser extraído por AI engines:

> "[Respuesta directa a la pregunta principal en 40-60 palabras. Sin relleno, sin \"en este artículo veremos\". Responde directamente, define el concepto, añade el dato más importante.]"

---
## Estructura del Contenido

### H1: [Keyword principal + promesa de valor]
*Intro (100-150 palabras):* Define el problema que resuelve el artículo. Incluye el snippet perfecto. No uses "En este artículo". Engancha con un dato o pregunta.

### H2: [Subtema 1 — fundamental para entender el tema]
*Desarrollo:* [qué debe cubrir, 200-300 palabras]
*Incluir:* [elemento específico: tabla, ejemplo, dato estadístico]

### H2: [Subtema 2]
*Desarrollo:* [qué debe cubrir]
*Incluir:* [elemento específico]

### H2: [Subtema 3 — diferenciador vs competencia]
*Desarrollo:* [ángulo único que competidores no cubren]

### H2: Preguntas Frecuentes (FAQ Schema)
#### H3: [Pregunta de PAA 1]
*Respuesta directa en 40-60 palabras:* [instrucción para el redactor]

#### H3: [Pregunta de PAA 2]
*Respuesta directa en 40-60 palabras:* [instrucción para el redactor]

#### H3: [Pregunta de PAA 3]
*Respuesta directa:* [instrucción]

### H2: Conclusión — [CTA implícito]
*Call to action:* [qué debe invitar a hacer — sin ser agresivo]

---
## Especificaciones de Contenido

| Elemento | Especificación |
|---|---|
| Extensión total | X palabras (±10%) |
| Tono | [profesional/conversacional/técnico] |
| Persona narrativa | Segunda persona (tú/usted) o tercera |
| Nivel técnico | Básico / Intermedio / Avanzado |
| Datos/estadísticas | Mínimo X citas con fuente |
| Imágenes | X imágenes con alt text descriptivo |
| Links internos | Mínimo X links a otras páginas del sitio |
| Links externos | X links a fuentes autoritativas (.edu, .gov, publicaciones) |

---
## Elementos Obligatorios para AEO
- [ ] Snippet perfecto en los primeros 100 palabras.
- [ ] Al menos 3 preguntas respondidas directamente con H3.
- [ ] FAQ Schema implementado en las preguntas.
- [ ] Definición clara del concepto principal (citabilidad).
- [ ] Datos numéricos con fuente (las AI citan datos verificables).
- [ ] Autor visible con bio (E-E-A-T).
- [ ] Fecha de publicación y última actualización visible.

---
## Competidores a Superar

| URL competidor | Fortalezas | Cómo superarlo |
|---|---|---|
| [url] | [qué hace bien] | [ángulo diferenciador] |
| [url] | [qué hace bien] | [ángulo diferenciador] |

---
## Lo Que NO Debe Incluir
- Frases de relleno: "En la era digital de hoy en día", "Es importante destacar que".
- Promesas sin evidencia: "el mejor", "el más completo".
- Secciones sin fuente si incluye estadísticas.
- Más de 3 niveles de encabezado (H1-H2-H3).

---
## Schema Markup a Implementar

```json
{
  "@type": "[Article/FAQPage/HowTo]",
  "headline": "[H1]",
  "datePublished": "YYYY-MM-DD",
  "author": {"@type": "Person", "name": "[Nombre del autor]"}
  // Para FAQ: añadir cada pregunta/respuesta
}
```

## Checklist Pre-Publicación
- [ ] Keyword principal en H1, primer párrafo, meta title, meta description.
- [ ] Snippet perfecto está en los primeros 100 palabras.
- [ ] Todas las FAQ tienen respuestas directas ≤60 palabras.
- [ ] Links internos y externos incluidos.
- [ ] Imágenes con alt text.
- [ ] Schema markup implementado.
- [ ] Fecha de publicación y autor visibles.

## Anti-Patterns
- ❌ **No crear briefs sin estructura H1-H2-H3 explícita** — el redactor toma decisiones equivocadas de jerarquía.
- ❌ **No olvidar el snippet perfecto** — es el elemento más importante para AEO y generalmente se omite.
- ❌ **No pedir más de 5,000 palabras si no lo justifica el SERP** — contenido largo por contenido largo no rankea.
- ❌ **No crear FAQ sin schema markup** — sin schema las preguntas no aparecen en featured snippets.
- ❌ **No omitir links internos** — son críticos para SEO y generalmente se olvidan en el brief.

## Done Criteria
- [ ] Meta title y description listos para copiar-pegar (sin editar).
- [ ] Snippet perfecto redactado (no "instrucción para redactar snippet").
- [ ] Estructura H1-H2-H3 completa con instrucciones por sección.
- [ ] FAQ con mínimo 3 preguntas y guía de respuesta ≤60 palabras.
- [ ] Lista de competidores con ángulo diferenciador.
- [ ] Schema markup especificado.
- [ ] Checklist pre-publicación incluida.
