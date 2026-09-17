#!/usr/bin/env bash
# Rebuild the whole gyrfalcon project: Python deps + the web dashboard + the TUI.
#
# Why this exists: `gyrfalcon dashboard` serves the prebuilt static bundle in
# `web/dist/`, not the `.tsx` source live — editing web/src alone does nothing
# until that bundle is rebuilt, and a browser tab already open won't notice a
# new bundle exists until it's reloaded. This script is the one command that
# makes sure nothing is left stale.
#
# Usage: ./rebuild.sh [--skip-tui]

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

skip_tui=false
for arg in "$@"; do
  case "$arg" in
    --skip-tui) skip_tui=true ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

echo "==> [1/3] Syncing Python dependencies (uv sync --extra dev)"
# --extra dev, not a bare `uv sync`: a bare sync prunes the environment down to
# the base dependencies, uninstalling pytest/ruff/mypy and breaking the test
# workflow the next time you reach for it.
uv sync --extra dev

echo "==> [2/3] Building web dashboard (web/)"
(cd web && npm install --no-audit --no-fund && npm run build)

if [ "$skip_tui" = false ]; then
  echo "==> [3/3] Building TUI (ui-tui/)"
  (cd ui-tui && npm install --no-audit --no-fund && npm run build)
else
  echo "==> [3/3] Skipping TUI build (--skip-tui)"
fi

echo
echo "==> Rebuild complete."
bundle=$(ls web/dist/assets/index-*.js 2>/dev/null | head -1)
if [ -n "${bundle:-}" ]; then
  echo "    Dashboard bundle: $bundle"
  echo "    If gyrfalcon dashboard is already running, hard-refresh the browser tab"
  echo "    (Ctrl+Shift+R / Cmd+Shift+R) — an open tab keeps the old bundle in memory"
  echo "    until reloaded; the server itself needs no restart for static files."
fi
