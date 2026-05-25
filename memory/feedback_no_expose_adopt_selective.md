---
name: feedback-no-expose-adopt-selective
description: Dr. Strange queda privado/local — no exponer vía MCP, API, ni nombre en content público. Adopt tools + methodology de practitioners (Steipete, Uncle Bob, Bin Liu) selectivamente sin romper el core.
metadata:
  type: feedback
---

2026-05-23: Hector estableció framework operacional para evolución de Dr. Strange tras inspección de stack Steipete/OpenClaw (374K stars OSS).

**Why:** Dr. Strange es activo competitive personal de Hector, construido en 6 meses de fine-tuning custom a su workflow específico. Exponerlo erosiona el valor diferencial. Pero practitioners como Steipete, Uncle Bob Martin, Bin Liu sí han construido cosas reales que valen la pena estudiar e integrar selectivamente.

**How to apply:**

1. **NO exponer Dr. Strange como producto:**
   - No publicar como MCP server (cancelado mcporter expose path 2026-05-23).
   - No abrir endpoints públicos del daemon (mantiene Telegram + web chat 127.0.0.1).
   - No publicar el repo público con artifacts internos (memory/, MEMORY.md, SOUL.md privados).
   - No mencionar "Dr. Strange" por nombre en content público (LinkedIn / X / blog). Usar terminología genérica como "my own agent system" / "a personal AI ops layer" cuando se necesite credibilidad de practitioner.

2. **Adopt selective de herramientas externas que mejoran capability sin reemplazar core:**
   - **Peekaboo** (OpenClaw, 4.5K⭐): macOS UI automation accessibility-first. Reemplaza screencapture + pyautogui actuales. Install: `brew install steipete/tap/peekaboo`.
   - **agent-scripts pointer pattern** (Steipete, 3K⭐): canonical AGENTS.MD en un repo, pointer-style en downstream repos. Aplica para escalar a multi-repo (Pachano Design + AI Lead Gen + Tatiana Insurance + QTS).
   - Otras herramientas a evaluar caso-por-caso (HyperFrames OSS cuando salga, AXorcist, etc).

3. **Aprender methodology sin copiar implementation:**
   - "Review your rules" prefix anti-drift (Uncle Bob).
   - pending-messages/ priority queue pattern (Uncle Bob).
   - VISION.md per-repo (Steipete).
   - Constitution priority resolution explicit (Uncle Bob).
   - Skills system con YAML frontmatter (Steipete agent-scripts).

4. **NO migrar Dr. Strange a otro stack:**
   - No reescribir en Node/TypeScript para imitar OpenClaw.
   - No depender de runtime externo (mantener Python daemon local con Telegram + web).
   - Cualquier herramienta adoptada debe coexistir, no reemplazar core.

**Rule de evaluación para futuras herramientas/methodology:**
- ¿Mejora capability de Dr. Strange sin exponerlo? → Adopt.
- ¿Es methodology que aplica al sistema sin reemplazarlo? → Aprender.
- ¿Requiere exponer Dr. Strange (MCP server, API, public repo)? → Reject.
- ¿Requiere migration completa de stack? → Reject.
- ¿Mejora otro repo personal (Pachano Design, AI Lead Gen, etc)? → Evaluate aparte, no parte de Dr. Strange.

**Linked:** [[project-weekly-content-cadence]] aplicar terminología genérica en Semana 2 LinkedIn/X content. [[feedback-verify-publish-actually-succeeded]] aplica al publish flow.
