#!/usr/bin/env bash
# Starts the Jarvis backend and opens the control panel in your browser.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "==> .venv not found, running setup first"
  ./scripts/setup.sh
fi

# shellcheck disable=SC1091
source .venv/bin/activate

exec python -m jarvis serve "$@"