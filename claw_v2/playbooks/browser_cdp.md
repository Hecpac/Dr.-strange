---
name: Browser CDP Automation
triggers:
  - chrome
  - cdp
  - browser
  - playwright
  - selenium
  - navega
  - abre chrome
priority: 5
---

# Chrome CDP — Automatización de Browser

## Setup
- Chrome CDP usa perfil dedicado en `~/.claw-chrome-cdp`
- Lanzar: `bash scripts/launch-chrome-cdp.sh` (puerto 9222 por defecto)
- Chrome 146+ bloquea CDP en perfil default — siempre usar user-data-dir dedicado

## Conexión
```python
from playwright.sync_api import sync_playwright
pw = sync_playwright().start()
browser = pw.chromium.connect_over_cdp("http://localhost:9222")
context = browser.contexts[0]
# Listar tabs
for i, p in enumerate(context.pages):
    print(f"Tab {i}: {p.url} - {p.title()}")
```

## Patrones Útiles
- **Screenshot**: `page.screenshot(path="/tmp/screenshot.png")` luego `Read` para ver
- **Click seguro**: `page.mouse.click(x, y)` cuando `.click()` falla por intercepted events
- **Angular forms**: `keyboard.type(text, delay=25)` en vez de `fill()` para trigger change detection
- **Esperar elemento**: `page.wait_for_selector("selector", timeout=10000)`
- **Contenido dinámico**: Espera extra (1.5s) para X, Instagram, Facebook, Reddit, LinkedIn

## Gotchas
- `DevBrowserService.chrome_navigate()` cierra conexión al terminar — usar Playwright directo para multi-step
- Cookie files encriptados per-profile — no se pueden copiar entre perfiles
- Siempre usar `open -na` (no `-a`) para Chrome con flags
- Session de Google login persiste en `~/.claw-chrome-cdp` tras login manual inicial
