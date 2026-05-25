---
name: feedback-verify-publish-actually-succeeded
description: Después de cualquier publicación (LinkedIn, X, comment, reply, post), verificar que el artefacto exista realmente en la UI rendered — nunca declarar éxito por "texto encontrado en DOM"
metadata:
  type: feedback
---

2026-05-22: Hector corrigió tras intento fallido en post de Andrew Ng. Yo declaré "comment publicado" porque encontré el texto en `body.innerText`, pero el texto estaba en el comment editor todavía abierto, no en la lista publicada. Hector lo notó porque revisó su LinkedIn y "el más reciente fue hace 3 h" — mi comment no aparecía.

**Why:** "Texto en DOM" ≠ "artefacto publicado". El texto puede estar:
- En el editor contenteditable (typed pero no submitted)
- En un draft cache
- En un modal abierto
- En el placeholder text de un editor

Sólo está PUBLICADO si aparece dentro del container de artefactos rendered de esa plataforma (ej: `<article class="comments-comment-entity">` para LinkedIn comments, tweet card en X feed, etc).

**How to apply:**

Después de cada acción de publicación, verificar con selectors específicos al tipo de artefacto:

| Plataforma | Acción | Selector de verificación |
|---|---|---|
| LinkedIn post | publish | `[data-urn*="activity:"]` con texto del post + URL contains `/feed/update/urn:li:activity:` |
| LinkedIn comment | publish | `article.comments-comment-entity` con el texto del comment |
| LinkedIn reply | publish | `article.comments-comment-entity` con texto, anidado bajo el parent comment |
| X tweet | publish | `article[data-testid="tweet"]` con el texto en el feed/profile |
| X thread | publish | múltiples `article[data-testid="tweet"]` consecutivos del autor |
| X reply | publish | `article[data-testid="tweet"]` con texto en el reply chain del tweet padre |

Si el primer intento de submit (Cmd+Enter, keyboard shortcut) NO produce el artefacto verificable en DOM rendered, retry con el método alternativo (explicit button click). NUNCA reportar éxito sin verificación específica.

**Especificidad por plataforma (memoria técnica 2026-05-22):**

- **LinkedIn comment editor:** Cmd+Enter NO funciona como submit. Requiere click explícito al botón "Comment" del form (no el button "Comment" del toolbar del post, que sólo abre el editor). El submit button vive dentro de `form` o `div[class*="comment-box"]` con texto exact "Comment" / "Publicar" / "Reply" / "Comentar".
- **LinkedIn post modal:** SÍ acepta click del Post button. Cmd+Enter no probado.
- **X tweet/thread:** Cmd+Enter SÍ funciona. Backup: `button[data-testid="tweetButton"]` o `button[data-testid="tweetButtonInline"]`.
- **X reply:** Cmd+Enter SÍ funciona. Backup: mismo testid.

**Workflow correcto post-publish:**

1. Submit (preferir explicit button click sobre keyboard shortcut salvo verificación documentada).
2. Esperar 5-7s para que UI procese.
3. Re-query DOM con selector específico al artefacto.
4. Si dom_matches > 0 + match dentro del container correcto → reportar éxito.
5. Si dom_matches = 0 → retry con submit method alterno + re-verificar.
6. Si 3 retries fallan → reportar fallo honesto y devolver al usuario.

**Linked:** [[project-weekly-content-cadence]] aplicar este protocolo a cada LinkedIn post + X thread + cross-link comment de la cadencia semanal.
