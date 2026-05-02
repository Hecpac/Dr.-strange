#!/usr/bin/env bash
set -euo pipefail
sudo chown -R "$(id -un):admin" /opt/homebrew/lib/node_modules/@google
npm install -g @google/gemini-cli
npm list -g --depth=0 @google/gemini-cli
