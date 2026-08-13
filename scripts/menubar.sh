#!/usr/bin/env bash
# macOS menu-bar launcher for Jarvis. Keeps the server alive in the background
# and exposes quick actions: open UI, restart, doctor, quit.
#
# Install once:   ln -sf "$(pwd)/bin/Jarvis.app" /Applications
# Or run directly: ./bin/jarvis-menubar.py
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  ./scripts/setup.sh
fi

# shellcheck disable=SC1091
source .venv/bin/activate

exec python bin/jarvis-menubar.py "$@"