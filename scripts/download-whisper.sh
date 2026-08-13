#!/usr/bin/env bash
# Pre-downloads the Whisper STT model (via HF mirror) so the first
# transcription doesn't block on a big download.
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate

export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
MODEL="${1:-${JARVIS_WHISPER_MODEL:-large-v3-turbo}}"

echo "==> downloading Whisper model '${MODEL}' via ${HF_ENDPOINT} ..."
python - <<PY
from faster_whisper import WhisperModel
m = WhisperModel("${MODEL}", device="cpu", compute_type="int8")
print("==> Whisper model ready:", "${MODEL}")
PY