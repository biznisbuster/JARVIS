#!/usr/bin/env bash
# Bootstraps Jarvis: creates venv, installs Python deps, installs Kilo CLI,
# downloads a Piper Serbian voice (best-effort), and starts the server.
#
# Re-runnable. Reuses existing venv if present.
set -euo pipefail

cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
VENV=".venv"
PORT="${JARVIS_PORT:-7777}"

echo "==> python: $($PY --version)"

if [ ! -d "$VENV" ]; then
  echo "==> creating venv ($VENV)"
  $PY -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "==> upgrading pip"
python -m pip install --upgrade pip wheel >/dev/null

if ! command -v brew >/dev/null 2>&1; then
  echo "==> Homebrew not found; assuming portaudio is already available."
else
  if ! brew list portaudio >/dev/null 2>&1; then
    echo "==> installing portaudio (sounddevice backend)"
    brew install portaudio
  fi
fi

echo "==> installing python requirements"
python -m pip install -r requirements.txt

if ! command -v node >/dev/null 2>&1; then
  echo "==> Node.js missing. Install Node 18+ to get Kilo CLI: https://nodejs.org"
else
  if ! command -v kilo >/dev/null 2>&1; then
    echo "==> installing kilo CLI (@kilocode/cli)"
    npm install -g @kilocode/cli || echo "   ! kilo install failed; you can still run Jarvis without it."
  else
    echo "==> kilo already installed: $(command -v kilo)"
  fi
fi

echo "==> initializing .env"
if [ ! -f .env ]; then
  cp .env.example .env
else
  # Preserve user values for known secrets, refresh the rest from .env.example.
  .venv/bin/python - <<'PY'
import re, pathlib
ex = pathlib.Path('.env.example').read_text()
cur = pathlib.Path('.env').read_text() if pathlib.Path('.env').exists() else ''
keep = {}
for line in cur.splitlines():
    if '=' in line and not line.strip().startswith('#'):
        k, v = line.split('=', 1)
        if 'KEY' in k or 'PASSWORD' in k or 'TOKEN' in k or 'SECRET' in k:
            keep[k.strip()] = line
out_lines = []
for line in ex.splitlines():
    if line.strip().startswith('#') or '=' not in line:
        out_lines.append(line); continue
    k = line.split('=', 1)[0].strip()
    if k in keep:
        out_lines.append(keep[k])
    else:
        out_lines.append(line)
pathlib.Path('.env').write_text('\n'.join(out_lines) + '\n')
print('  .env reconciled')
PY
fi

echo
echo "==> Jarvis setup complete. Start with:  ./scripts/start.sh"
echo "    Or:  $VENV/bin/python -m jarvis serve"