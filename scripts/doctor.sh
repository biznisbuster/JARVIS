#!/usr/bin/env bash
# Quick health check — same data the UI shows under "Konekcije".
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
exec python -m jarvis doctor