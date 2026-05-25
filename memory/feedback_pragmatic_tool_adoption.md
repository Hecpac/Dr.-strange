---
name: feedback-pragmatic-tool-adoption
description: Adoptar herramientas solo donde demuestran valor empírico real; descartar cuando no encajan, sin sunk-cost fallacy
metadata:
  type: feedback
---

2026-05-23: Hector estableció principle pragmático después de testear Peekaboo y descubrir que no aplica para LinkedIn/X dentro de Chrome.

**Rule:** *"Vamos a usarlo donde nos ayuda y si no es una opción buena la desechamos."*

**Why:** Evita el sunk-cost fallacy con tools instaladas. Si una tool no demuestra valor en su lane intentada, no forzar uso solo porque ya está instalada. Mejor saberlo rápido y mover a la siguiente que sí encaja.

**How to apply:**

1. **Test empírico antes de refactor amplio.** Cuando se evalúa una tool nueva, hacer 1 test puntual en un caso real ANTES de refactorear scripts existentes. Si el test no es claro, no hacer batch refactor.

2. **Document tanto los wins como los misses.** Cuando una tool no sirve para un use case, registrar la limitation con evidence (rc, stderr, comportamiento observado). No quedarse "supongo que sí sirve". Ejemplo Peekaboo: NO sirve para Chrome web content (rc=1 en `see --app 'Google Chrome'`, window titles "?", `browser` subcommand no existe), SÍ sirve para native macOS apps.

3. **Mantener instalada la tool aunque sea limitada.** No desinstalar si tiene zona de utilidad real, aunque sea acotada. Peekaboo se queda instalado para use cases donde brilla (system dialogs, app launch/quit/switch, menu bar, paste con clipboard preserve), aunque NO se use para Chrome.

4. **No forzar adoption por hype/stars.** Una tool puede tener miles de stars y ser excelente en su domain, pero NO ser fit para tu workflow específico. Steipete's Peekaboo (4.5K⭐) tiene un público real, pero LinkedIn/X dentro de Chrome no es su caso de uso. Reconocer eso sin defensividad.

5. **El framework "adopt selective + preserve core" ([[feedback-no-expose-adopt-selective]])** se complementa con éste. Adopt selective implica que NO toda tool aplicada va a integrarse — algunas fallan al test empírico y se descartan.

**Linked:**
- [[feedback-no-expose-adopt-selective]] el framework operacional macro.
- [[feedback-verify-before-denying]] reverso útil: tampoco descartar antes de testear.
