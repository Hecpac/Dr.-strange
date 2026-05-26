# Social Media Engagement Agent — MVP

Este paquete recupera la petición que falló: **crear un agente para redes sociales con skills orientadas a engagement**.

Incluye:

- `social_media_agent.py`: agente MVP sin dependencias externas.
- `agent_spec.yaml`: contrato declarativo del agente, skills, gates y métricas.
- `runtime_guards.py`: parches operativos para los problemas detectados en la auditoría:
  - saneamiento de imágenes rotas en el contexto,
  - watchdog de Chrome/CDP,
  - degradación Realtime TTS → batch,
  - gate Tier-3 menos invasivo,
  - filtro de español neutral latam sin voseo.
- `postmortem_actions.md`: backlog accionable priorizado.
- `test_smoke.py`: prueba rápida del MVP.

## Uso rápido

```bash
python social_media_agent.py --platform instagram --topic "lanzamiento de una newsletter de IA para founders" --goal "comentarios y guardados" --audience "founders LATAM"
```

## Integración recomendada

1. Ejecuta `sanitize_llm_messages(...)` antes de cada llamada al modelo.
2. Si el modelo devuelve un error de imagen inválida, reintenta una sola vez con `drop_all_images=True`.
3. Usa `RealtimeTTSGuard` para desactivar realtime al detectar `beta_api_shape_disabled`.
4. Usa `tier3_requires_confirmation(...)` para evitar pedir 3 decisiones cuando el usuario ya abrió sesión o dejó la app lista.
5. Ejecuta `neutral_latam_rewrite(...)` antes de enviar respuestas en español.
