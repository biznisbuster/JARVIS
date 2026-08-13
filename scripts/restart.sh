#!/usr/bin/env bash
# Re-starts the Jarvis backend.
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate

PID=$(lsof -nP -iTCP:"${JARVIS_PORT:-7777}" -sTCP:LISTEN -t 2>/dev/null || true)
if [ -n "$PID" ]; then
  echo "==> stopping existing Jarvis (pid $PID)"
  kill "$PID" || true
  sleep 1
fi
exec python -m jarvis serve "$@"