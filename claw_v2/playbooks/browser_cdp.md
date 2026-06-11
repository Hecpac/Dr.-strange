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
- Chrome CDP lo administra el daemon con `ManagedChrome` en `http://localhost:9250`.
- El perfil dedicado es `~/.claw/chrome-profile`.
- Verificar disponibilidad con `/chrome_pages` o `curl http://localhost:9250/json/version`.
- Para abrir Chrome visible y hacer login: usar `/chrome_login`; al terminar, volver a headless con `/chrome_headless`.
- No lanzar Chrome con scripts puente. Chrome 146+ bloquea CDP en perfil default, así que siempre usar el perfil administrado.

## Conexión
```python
from playwright.sync_api import sync_playwright
pw = sync_playwright().start()
browser = pw.chromium.connect_over_cdp("http://localhost:9250")
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
- Session de Google login persiste en `~/.claw/chrome-profile` tras login manual inicial.
- Si CDP no está disponible, usar `/chrome_login` o revisar el estado del daemon; no iniciar un segundo Chrome por fuera.
