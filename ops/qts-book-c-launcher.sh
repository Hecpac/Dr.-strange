#!/bin/bash
# Launcher for QTS Book C 15m exploratory runner.
# Mirrors ops/qts-paper-ab-launcher.sh from the QTS-ARCHITECT repo.

set -euo pipefail

QTS_ROOT="$HOME/Projects/QTS-ARCHITECT"
DR_STRANGE_ROOT="$HOME/Projects/Dr.-strange"
LOG_DIR="$HOME/.claw"
mkdir -p "$LOG_DIR"

cd "$QTS_ROOT"

exec "$QTS_ROOT/.venv/bin/python" "$DR_STRANGE_ROOT/scripts/qts_book_c_gold_15m_runner.py" \
    >> "$LOG_DIR/qts-book-c-runner.stdout.log" \
    2>> "$LOG_DIR/qts-book-c-runner.stderr.log"
