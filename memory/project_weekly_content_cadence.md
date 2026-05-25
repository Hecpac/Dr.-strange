---
name: project-weekly-content-cadence
description: Compromiso semanal de publicación en LinkedIn + X anchored en cuadernos NotebookLM y experiencia construyendo Dr. Strange
metadata:
  type: project
---

2026-05-22: Hector confirmó compromiso de cadencia semanal de contenido en LinkedIn + X.

**Why:** Personal brand differentiation como AI builder (no coach/consultant). Compound visibility para job search Fase 1 + AI Lead Gen service marketing + thought leadership. La hipótesis: cada semana publicando insight basado en evidencia real (Dr. Strange + research NotebookLM) compone reach durante meses sin pagar ads. Cadencia fija, no negociar cada semana si publicar o no.

**How to apply:**

- **Plataformas:** LinkedIn personal profile (Hector Pachano) + X (`@PachanoDesign`).
- **Cadencia base:** 1 LinkedIn post + 1 X thread por semana. Misma idea central, dos formatos.
- **Días:** Tuesday US Central — LinkedIn 9am, X 11am (2h spread para no canibalizar engagement).
- **Source:** principalmente NotebookLM cuadernos + lived Dr. Strange experience. NUNCA contenido genérico. Siempre anchor verificable (commit, fix, fail observed, cita real).
- **Voz:** español neutro LatAm para X cuando audiencia LatAm; inglés para LinkedIn (audiencia US AI builders). Sin voseo nunca.
- **No-go:** posts motivacionales, listas genéricas, sales pitches frontales, contenido sin defensa técnica.

**Done so far:**

- 2026-05-22 (Tuesday): V1 LinkedIn published — "I built an AI agent that runs my business 24/7" / Instruction Layering / AGENTS.md angle / 380 words. Toast LinkedIn "Post successful" confirmado. Pendiente: X thread misma semana.

**Backlog inmediato (de las 51 fuentes del cuaderno NotebookLM "Sistemas Multiagente y Arquitecturas de Memoria en IA"):**

1. Token Tax 15x — Anthropic data: single agent 4x, multi 15x. Por qué multi-agent es architectural failure en tareas low-stakes.
2. SKILL0 Internalization — Visual context (RGB image tokens) vs RAG-stuffing. <0.5k tokens/step.
3. Forgetting Paradox — "Markdown is memory", Stateless Hub architecture. Statelessness as feature.
4. Reliability Gap / Tool Metadata — Agents like microservices, staggered deployment.
5. Karpathy's LLM Wiki vs SOUL.md — Personal LLM knowledge base build.
6. AGENTS.md como protocolo cross-tool — Cursor, Aider, Claude Code, Codex convergiendo.
7. The "Easy Button" Mirage — Por qué ninguna agency vendor vende un agent ready-to-deploy que funcione.
8. Session Amnesia → Durable Files — extension natural del V1.

**Próximos cuadernos NotebookLM como future source:**
- Job search & AI Engineer hiring debate (Gordon DuQuesnay, METR, Mercor)
- AI Lead Gen / vertical SaaS playbook
- Voice + video AI pipelines (HeyGen, ElevenLabs, Inworld, Sora 2)

**Producción operativa (workflow propuesto, requiere OK de Hector):**

- Domingo PM: Dr. Strange selecciona tema del backlog, crea draft inicial v1 (LinkedIn long-form + X thread). Hector revisa lunes.
- Lunes AM: Hector edita / aprueba / pide iterar.
- Lunes PM: Draft final guardado en `artifacts/content/YYYY-MM-DD-tema/`.
- Tuesday 9am CT: Dr. Strange publica LinkedIn via Chrome CDP.
- Tuesday 11am CT: Dr. Strange publica X thread via CDP o API.
- Tuesday 12pm CT: Dr. Strange notifica Hector + monitorea engagement primera hora.
- Tuesday 6pm CT: Reporte de engagement (likes, comments, impressions).

**Métricas a trackear (operativo, no público):**
- LinkedIn: impressions, click rate, comments, profile visits WoW.
- X: impressions, engagement rate, reply rate, follower growth.
- Conversion: DMs / job inquiries / AI Lead Gen leads.
- Si 8 semanas sin tracción → iterar formato o nicho, no abandonar cadencia.

**Memoria de UI técnica (LinkedIn 2026):**
- LinkedIn Post button modal NO matchea `aria-label="Post"` ni `get_by_role(button, name='Post')` — selectors fallan.
- Workaround verificado: click coordenada (1010, 482) en viewport 1400×950 — Post button está consistentemente bottom-right del modal.
- Premium modal "Don't miss your next opportunity" aparece a veces al cargar feed; dismiss con `button[aria-label*="Dismiss"]` (4 matches encontrados típicamente).
- Composer textbox: `div[contenteditable="true"][role="textbox"]` — `page.keyboard.insert_text()` preserva emojis y newlines correctamente.
