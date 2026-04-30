# Claw Evolution Plan — Overview

Last revised: 2026-04-28

## Objetivo

Construir un sistema de ejecución más robusto, verificable y menos propenso a drift,
donde cada acción esté anclada al Goal Contract, a evidencia trazable y a checkpoints
de crítica protocolizados.

El núcleo del diseño es:

1. Goal Contract
2. Evidence Ledger
3. Typed Action Events
4. Append-only JSONL logs con redaction y schema versioning
5. GDI-lite (Goal Drift Index) en log-only, luego calibrated gating
6. Critic protocolizado (in-process v0, async external después)
7. Active Recall / Reflexion-style episodic memory con quality gate
8. FaR (Fact-and-Reflection) structured doubt flags + tool-grounded confidence

La idea de "dos procesos Claude en paralelo" queda como una etapa posterior
(P4), no como el centro del plan.

## Principios de diseño

- El **Goal Contract** es el ancla principal del sistema, no el primer mensaje del usuario.
- Toda claim importante debe estar respaldada por **tool evidence** o marcada
  explícitamente como **inferencia / asunción**.
- El crítico no opina: emite una decisión estructurada `approve | revise | block | ask_human`
  con `reason_summary`, `goal_alignment` (NO `drift_score` para evitar colisión con GDI),
  `required_fix` y `risk_assessment`.
- El sistema debe poder auditar:
  - qué se intentó,
  - por qué se intentó,
  - con qué evidencia,
  - qué cambió,
  - y qué señales de drift aparecieron.
- Durante calibración, el drift monitor **observa y registra**, pero no bloquea por defecto.
- Las señales primarias del GDI son **acciones, tool calls, file edits, claims y risk
  escalations** — nunca thoughts privados.
- Los drift logs viven como JSONL redactado bajo `~/.claw/telemetry/`. Solo lecciones
  revisadas se destilan hacia `MEMORY.md`.
- Todo artefacto persistido lleva `schema_version` para permitir migración futura.

## Orden final de fases

| Fase | Entregables |
|------|-------------|
| P0 | Goal Contract + Evidence Ledger + Typed Action Events + append-only JSONL + redaction + schema_version |
| P1 | GDI-lite log-only |
| P1b | Calibrated gating para Tier-2/Tier-3 |
| P2 | Critic Checkpoints v0 (in-process) |
| P3 | Active Recall / Reflexion-style episodic memory con quality gate |
| P4 | Async external critic para Tier-2/Tier-3 |
| P5 | FaR structured doubt flags + tool-grounded confidence |
| P6 | Adopciones derivadas de OpenClaw 2026.4.26 (hardening operativo) |

## Reglas duras

- No usar el primer mensaje del usuario como anchor principal — usa Goal Contract.
- No observar thoughts como señal principal — usa typed action events.
- No escribir drift logs en `MEMORY.md`.
- No usar thresholds GDI como hard gates hasta haberlos calibrado con datos reales.
- No nombrar el `drift_score` del crítico igual que el GDI — usar `goal_alignment`
  o `gdi_snapshot` para distinguir el estado puntual de la lectura del Critic.
- Incluir `ask_human` como decisión válida del crítico.
- `schema_version` obligatorio en: Goal Contract, Evidence Ledger, Events, GDI snapshots,
  Critic Decisions, Recall Results.

## Documentos hermanos

- `01-goal-contract.md`
- `02-evidence-ledger.md`
- `03-typed-action-events.md`
- `04-gdi.md`
- `05-critic-protocol.md`
- `06-active-recall.md`
- `07-far-doubt-flags.md`
- `08-storage-and-redaction.md`

## Criterios de éxito por fase

- **P0**: cada tarea tiene contrato activo, claims importantes tienen trazabilidad,
  los eventos quedan tipados y persistidos en JSONL versionado.
- **P1**: el GDI se calcula y registra, sin alterar comportamiento.
- **P1b**: el gating Tier-2/Tier-3 usa señal calibrada, no thresholds rígidos sin contexto.
- **P2**: el crítico in-process emite decisiones en formato estricto y se respeta su veredicto.
- **P3**: la memoria episódica solo se consolida si supera el quality gate; recall
  devuelve hits relevantes para tareas medium/high risk.
- **P4**: el crítico asincrónico externo puede bloquear o revisar con el mismo contrato,
  sin acceso a chain-of-thought privado.
- **P5**: la confianza reportada está amarrada a evidencia y duda estructurada;
  el sistema "expresa preocupación" en lugar de afirmar con sobreconfianza.
- **P6**: items A (EPIPE handling) y E (token redaction) implementados; items
  C (profile scoping), D (subagent allowlist), B (atomic install), F (realpath
  cache) en backlog explícito o ejecutados según necesidad. Ver `09-openclaw-derived-adoptions.md`.
