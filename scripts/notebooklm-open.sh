#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec ./.venv/bin/python -m claw_v2.browser_cli '{"action":"goto","url":"https://notebooklm.google.com/"}'
