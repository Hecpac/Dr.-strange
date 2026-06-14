#!/bin/bash
# Recicla el Chrome de automatización (que corre headless/invisible) a una
# ventana VISIBLE con la misma sesión @pachanodesign, abierta en Instagram,
# y deja CDP en :9250 para que Dr. Strange siga manejándolo.
#
# Uso (una sola vez, desde Terminal en la Mac):
#   bash /Users/hector/Projects/Dr.-strange/scripts/show_ig_visible.sh

set -u
PROFILE="$HOME/.claw/chrome-profile"

echo "1/3 Cerrando el Chrome headless en :9250 (la sesión queda guardada en disco)..."
pkill -f "remote-debugging-port=9250" 2>/dev/null || true
sleep 2

echo "2/3 Quitando el lock del perfil..."
rm -f "$PROFILE/SingletonLock" 2>/dev/null || true

echo "3/3 Abriendo Chrome VISIBLE en Instagram (perfil @pachanodesign, CDP :9250)..."
open -na "Google Chrome" --args \
    --remote-debugging-port=9250 \
    --remote-allow-origins="http://127.0.0.1:9250,http://localhost:9250" \
    --user-data-dir="$PROFILE" \
    --no-first-run \
    --start-maximized \
    --window-position=0,0 \
    --window-size=1800,1100 \
    "https://www.instagram.com/"

osascript \
    -e 'tell application "Finder" to set desktopBounds to bounds of window of desktop' \
    -e 'tell application "Google Chrome"' \
    -e 'activate' \
    -e 'if (count of windows) > 0 then set bounds of window 1 to desktopBounds' \
    -e 'end tell' >/dev/null 2>&1 || true

echo "Listo. Debería aparecer una ventana de Chrome con Instagram. Avísame y sigo desde ahí."
