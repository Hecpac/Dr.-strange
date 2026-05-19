#!/bin/bash
# Launcher for QTS Book D London+NY 1h exploratory runner.

set -euo pipefail

QTS_ROOT="$HOME/Projects/QTS-ARCHITECT"
DR_STRANGE_ROOT="$HOME/Projects/Dr.-strange"
LOG_DIR="$HOME/.claw"
mkdir -p "$LOG_DIR"

cd "$QTS_ROOT"

exec "$QTS_ROOT/.venv/bin/python" "$DR_STRANGE_ROOT/scripts/qts_book_d_gold_london_ny_runner.py" \
    >> "$LOG_DIR/qts-book-d-runner.stdout.log" \
    2>> "$LOG_DIR/qts-book-d-runner.stderr.log"
