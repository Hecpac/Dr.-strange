# Gemma 4 — Revisión del Plan v3

## Thinking (resumen)
Claw v2 (Dr. Strange), an autonomous AI assistant for Mac (Telegram controlled).
Python 3.12, SQLite, asyncio, multi-LLM.

        *   ~17k LOC production code, 22k LOC unit tests.
        *   *Zero* end-to-end (E2E) tests.
        *   God objects: `brain.py`, `bot.py`, `memory.py`, `agents.py`.
        *   Complex features: Multi-surface routing, Computer use, Browser automation, Job orchestration, Daemon management.
A 9-PR refactoring plan (PR#0 to PR#8).
Step-by-step analysis of the plan, providing best practices, optimal order, risks/mitigations, testing patterns, anti-patterns, rollback criteria, and "do not touch" areas.

    *   *PR#0 (Safety Net):* Essential. Without E2E tests, refactoring `brain.py` is suicide.
    *   *PR#1 (Brain Decomposition):* High risk. This is the core.
    *   *PR#2 (Bot Handler Registry):* Moderate risk. Mainly organizational.
    *   *PR#3 (Typed Artifacts):* High impact, medium risk. Changes how data flows. This actually supports PR#1 and PR#4.
    *   *PR#4 (Durable JobService):* High risk. State management and persistence changes.
    *   *PR#5 (OpenTelemetry):* Low risk, high value. Observability helps debugging later PRs.
    *   *PR#6 (ProcessManager):* Medium risk. Infrastructure layer.
    *   *PR#7 (Eval Harness):* Medium risk. New feature/framework.
    *   *PR#8 (VPS Spike):* Extreme risk. Architectural shift.

    *   *Dependency Analysis:*
        *   PR#3 (Typed Artifacts) provides the structure that PR#1 (Brain decomposition) and PR#4 (JobService) need. It's better to define the "language" (types) before splitting the "logic" (brain).
        *   PR#5 (OpenTelemetry) should come earlier. If I'm breaking the brain (PR#1), I want a trace of exactly where it failed.
        *   PR#0 must stay first.

    *   *Revised Order Proposal:*
        0 $\rightarrow$ 5 (Observability) $\rightarrow$ 3 (Types) $\rightarrow$ 1 (Brain) $\rightarrow$ 2 (Bot) $\rightarrow$ 4 (JobService) $\rightarrow$ 6 (Process) $\rightarrow$ 7 (Eval) $\rightarrow$ 8 (VPS).

    *   *Risk Mitigation:*
        *   Brain: Use "Parallel Run" (Shadowing) if possible, or very strict snapshot tests.
        *   Bot: Gradual migration of handlers.
        *   JobService: Idempotency is key.

    *   *Testing Patterns:*
        *   Snapshot testing (Goldens).
        *   Property-based testing (Hypothesis).
        *   Integration "canaries".

    *   *Anti-patterns:*
        *   The "Big Bang" (trying to do too much in one PR).
        *   "Refactoring while adding features".
        *   Over-engineering (too many abstractions).

    *   *Rollback:*
        *   Failure in snapshot tests.
        *   Regression in core "Computer Use" or "Memory".
        *   Performance degradation.

    *   *Do Not Touch:*
        *   `memory.py` (The user mentioned it's cohesive despite size; don't touch it unless necessary).
        *   `agents.py` (Cohesive, high value).
        *   The underlying SQLite schema (avoid migrations if possible during log

Este es un desafío de ingeniería clásico: refactorizar el "corazón" de un sistema en producción sin detener el latido. El plan v3 es sólido, pero tiene dependencias ocultas que podrían causar fricción.

Aquí tienes el análisis detallado y la propuesta de ejecución.

---

### 1. Orden Óptimo y Dependencias (Re-secuenciación)

El orden propuesto es lógico, pero hay una falla crítica: **estás intentando descomponer la lógica (`brain.py`) antes de definir el lenguaje de datos (`types.py`) y la visibilidad (`OpenTelemetry`)**.

Si descompones el `brain` sin tipos estrictos, solo estarás moviendo "diccionarios mágicos" de un archivo a otro, lo que dificulta el rastreo de errores.

**Orden Recomendado:**
1.  **PR#0 (Safety Net):** No negociable. Es la base.
2.  **PR#5 (OpenTelemetry):** **Mover aquí.** Necesitas trazabilidad *antes* de romper el `brain`. Si algo falla en PR#1, OpenTelemetry te dirá exactamente en qué span ocurrió.
3.  **PR#3 (Typed Artifacts):** **Mover aquí.** Define los contratos. Es mucho más fácil extraer `ContextAssembler` si este devuelve un `ContextArtifact` tipado que un `dict`.
4.  **PR#1 (Brain Decomposition):** Ahora tienes tests, trazas y tipos. El riesgo baja un 60%.
5.  **PR#2 (HandlerRegistry):** Limpieza de la superficie de entrada.
6.  **PR#4 (Durable JobService):** Esta es una funcionalidad nueva/evolucionada. Requiere que el `brain` ya esté estable.
7.  **PR#6 (ProcessManager):** Infraestructura.
8.  **PR#7 (Eval Harness):** Optimización de calidad.
9.  **PR#8 (VPS Spike):** El cambio arquitectónico más radical. Debe ser el último.

---

### 2. Riesgos Principales y Mitigaciones Concretas

| PR | Riesgo Principal | Mitigación Concreta |
| :--- | :--- | :--- |
| **#0** | Falsos positivos (tests que pasan pero el sistema falla). | Usar **"Golden Files"**: guardar el output exacto del LLM y el estado de la DB post-ejecución. |
| **#1** | Ruptura de la cadena de contexto (perder info entre componentes). | **Interface Locking**: Definir interfaces abstractas (`ABC`) antes de la extracción. |
| **#2** | Comandos que dejan de responder o pierden el `tier` de autonomía. | **Dual-Run**: Mantener el `bot.py` viejo y el nuevo registro en paralelo; loguear si ambos rutas coinciden. |
| **#3** | `TypeError` en tiempo de ejecución por payloads mal formados. | Usar **Pydantic v2** para validación estricta en los límites del sistema. |
| **#4** | Race conditions en la base de datos SQLite al reanudar jobs. | Implementar **Optimistic Locking** (versionado de filas) en la tabla de jobs. |
| **#8** | Latencia inaceptable en "Computer Use" (Mac $\leftrightarrow$ VPS). | Implementar un **Hybrid Agent Model**: El "Cerebro" en VPS, el "Brazo" (executor) local en Mac. |

---

### 3. Patrones de Testing Recomendados

Más allá de los unit tests, implementa esto por fase:

*   **Fase de Descomposición (PR#1, #2):**
    *   **Snapshot Testing:** Comparar el JSON de salida del `brain` antes y después de la refactorización con el mismo input.
    *   **Regression Suites:** Un set de 20 "casos críticos" (ej: "Toma una captura y resume el navegador") que deben funcionar idénticamente.
*   **Fase de Tipado (PR#3):**
    *   **Static Analysis:** Ejecutar `mypy` o `pyright` estrictamente. Si el tipado no pasa el check estático, el PR no se mergea.
*   **Fase de Persistencia (PR#4):**
    *   **Chaos Testing:** Matar el proceso `kill -9` mientras un Job está en estado `running` y verificar que el `JobService` lo recupere correctamente al reiniciar.

---

### 4. Buenas Prácticas de Ejecución

1.  **The "Strangler Fig" Pattern:** No borres la lógica vieja inmediatamente. Crea la nueva clase (`ContextAssembler`), instanciala dentro de `brain.py`, y redirige una sola función. Una vez validada, redirige la siguiente.
2.  **Feature Flags:** Para PR#2 y PR#4, usa flags (`ENABLE_NEW_REGISTRY = True`). Si el sistema falla en producción, un cambio de variable lo devuelve al estado anterior sin hacer rollback de código.
3.  **Atomic PRs:** Si un PR empieza a tocar más de 3 archivos no relacionados, divídelo. El PR#1 es peligroso precisamente porque es demasiado grande.

---

### 5. Anti-patrones a Evitar

*   **"The Big Bang Refactor":** Intentar hacer PR#1, #2 y #3 en un solo merge. Es la receta perfecta para un sistema caído durante 48 horas.
*   **Over-Abstraction:** No crees interfaces para cosas que nunca cambiarán. Si solo hay un `LLMRouter`, no necesitas una `AbstractRouter` compleja; una clase simple basta.
*   **Fixing Bugs while Refactoring:** Si encuentras un bug en `brain.py` mientras lo refactorizas $\rightarrow$ **Detente**. Crea un PR pequeño para arreglar el bug, mergealo, y luego sigue refactorizando. Mezclar "cambio de comportamiento" con "cambio de estructura" hace que los tests de snapshot sean inútiles.

---

### 6. Criterios de Rollback (Cuándo abortar)

Aborta el PR y revierte inmediatamente si:
1.  **Degradación de Latencia:** El tiempo de respuesta del `brain` aumenta $>20\%$ debido a la nueva arquitectura de capas.
2.  **Loss of Context:** El agente comienza a olvidar instrucciones del sistema que antes recordaba (indica error en `ContextAssembler`).
3.  **Deadlocks en Asyncio:** Aparición de `RuntimeError: Event loop is closed` o freezes que no estaban presentes.
4.  **Falla de Snapshot:** Si el output del sistema diverge en puntos críticos (ej: el agente decide usar una herramienta incorrecta).

---

### 7. Lo que NO tocar (Zonas Sagradas)

*   **`memory.py` (Core Logic):** Mencionaste que es cohesivo. No lo toques a menos que sea estrictamente necesario para el `JobService`. El riesgo de corromper la base de datos de hechos/embeddings es demasiado alto.
*   **`agents.py` (Agent Personalities):** La lógica de "quién es quién" (Hex, Rook, etc.) debe quedar intacta. La refactorización debe afectar al *cómo* se ejecutan, no al *qué* son.
*   **Esquema de SQLite actual:** Evita migraciones de base de datos en los primeros 4 PRs. Trabaja sobre el esquema existente para aislar los errores de lógica de los errores de persistencia.
