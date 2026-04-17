---
name: refactor-plan
description: >
  Identifica code smells sistémicos y produce un plan ordenado de pasos atómicos
  de refactoring. Usa este skill cuando se pida planificar un refactor, cuando
  code-review encuentre >3 SHOULD-FIX del mismo patrón en un PR, o cuando se
  detecte deuda técnica estructural. También aplica cuando se mencionen code smells,
  archivos demasiado grandes, duplicación, dependencias circulares, o cualquier
  solicitud de "refactorizar esto", "limpiar este módulo", o "plan de refactoring".
---

# Refactor Plan

Identifica code smells sistémicos y produce un plan ordenado de pasos de refactoring atómicos, donde cada paso es un commit independiente que pasa todos los tests.

## Inputs requeridos

Antes de ejecutar, recopila esta información:

| Input                          | Fuente                                           | Requerido      |
|-------------------------------|--------------------------------------------------|----------------|
| Raíz del codebase o directorios | Path del proyecto o directorios específicos        | ✅ Sí         |
| Hallazgos de code-review       | Output del skill code-review (si fue triggereado desde ahí) | Si disponible |
| Test suite                     | Ubicación de tests y comando para ejecutarlos      | ✅ Sí         |

Si no tienes acceso directo a las fuentes, pide al usuario que pegue o describa la información relevante.

## Proceso

### 1. Detección de smells
- Escanea buscando: duplicación (>20 líneas repetidas), god files (>500 líneas), imports circulares, nesting profundo (>4 niveles), listas de parámetros largas (>5 params).
- Para cada smell registra: tipo, ubicación exacta (file:line), impacto estimado.

### 2. Clustering
- Agrupa smells relacionados en temas de refactoring (e.g., "extraer servicio X", "dividir módulo Y").
- Cada tema debe tener un nombre descriptivo y la lista de smells que resuelve.

### 3. Análisis de dependencias
- Para cada tema, identifica qué archivos/funciones dependen del código que se va a modificar.
- Mapea las dependencias upstream y downstream de cada cambio propuesto.

### 4. Orden topológico
- Ordena los pasos: cambios sin dependientes downstream van primero.
- Garantiza que ningún paso dependa de un paso posterior.

### 5. Atomicity check
- Verifica que cada paso pueda ser un solo commit que pase todos los tests.
- Si un paso no puede ser atómico, divídelo en sub-pasos hasta que cada uno sea independiente.

## Output format

## 🔧 Refactor Plan: {tema o alcance}

### Smells Detected

| # | Type | Location | Impact |
|---|------|----------|--------|

### Plan (ejecutar en orden)

#### Step 1: {descripción}
- **Files**: {lista de archivos}
- **Change**: {qué hacer, específicamente}
- **Risk**: low | medium | high
- **Tests**: {qué tests cubren esto, o "necesita test nuevo: {descripción del test}"}
- **Commit message**: {mensaje sugerido}

#### Step 2: ...

### Dependencies
{Step N debe completarse antes que Step M porque...}

### Estimated Total Effort
{cantidad} steps, {evaluación de riesgo general}

## Done criteria
- [ ] Cada step es independientemente committable (tests pasan después de cada step).
- [ ] Ningún step depende de un step posterior.
- [ ] Evaluación de riesgo por step con mitigación si es medium/high.
- [ ] Si un step necesita un test nuevo, el test está especificado (no solo "agregar test").

## Errores comunes a evitar
- No propongas un step que rompe tests hasta que otro step posterior lo arregle.
  Cada step debe dejar el codebase verde de forma independiente.
- No seas vago en los cambios: "Refactorizar el módulo de pagos" no es un step.
  "Extraer PaymentValidator de payments.py:120-180 a validators/payment.py con tests en test_payment_validator.py" sí lo es.
- No ignores el orden de dependencias: si Step 3 mueve una función que Step 2 usa, Step 3 debe ir antes o Step 2 debe referenciar la nueva ubicación.
- No omitas risk assessment: cada step debe tener riesgo evaluado. Si es medium/high, incluye plan de mitigación concreto.
