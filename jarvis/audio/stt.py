"""Local Speech-to-Text.

Two backends, selected by ``SETTINGS.whisper.backend``:

* ``faster_whisper`` - CTranslate2, CPU (also NVIDIA CUDA). Default.
* ``mlx_whisper``    - Apple Silicon Metal GPU. Requires Python 3.10+ and
  ``pip install mlx-whisper``. Cheaper on battery, much faster on M-series.

Both lazy-load on first use. Serbian language is set explicitly to skip
auto-detect. ``warmup()`` transcribes a short silence at boot so the first
real voice message doesn't pay the model-load cost.
"""

from __future__ import annotations

import asyncio
import os
import struct
import tempfile
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .. import state as runtime_state
from ..bus import BUS
from ..config import SETTINGS

_MODEL_LOCK = asyncio.Lock()
_TRANSCRIBE_LOCK = asyncio.Lock()
# Whisper jobs are long-running and CPU/GPU-heavy; they must not starve the
# default executor that serves short osascript/clipboard calls.
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="whisper")


async def _get_model() -> tuple[str, Any]:
    """Return (backend_name, model_instance)."""
    state = runtime_state.whisper_state
    if state.model is not None:
        return SETTINGS.whisper.backend, state.model
    async with _MODEL_LOCK:
        if state.model is not None:
            return SETTINGS.whisper.backend, state.model
        backend = SETTINGS.whisper.backend
        await BUS.publish("whisper_loading", {"backend": backend, "model": SETTINGS.whisper.model})
        loop = asyncio.get_running_loop()

        if backend == "mlx_whisper":

            def _load_mlx() -> Any:
                import mlx_whisper  # noqa: F401  # type: ignore

                return SETTINGS.whisper.model

            state.model = await loop.run_in_executor(_EXECUTOR, _load_mlx)
            await BUS.publish(
                "whisper_ready", {"backend": "mlx_whisper", "model": SETTINGS.whisper.model, "device": "mps"}
            )
            return backend, state.model

        from faster_whisper import WhisperModel

        device = SETTINGS.whisper.device
        if device == "auto":
            try:
                import torch  # noqa: F401

                device = "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:  # noqa: BLE001
                device = "cpu"
        compute_type = SETTINGS.whisper.compute

        def _load_fw() -> Any:
            return WhisperModel(
                SETTINGS.whisper.model,
                device=device,
                compute_type=compute_type,
            )

        state.model = await loop.run_in_executor(_EXECUTOR, _load_fw)
        await BUS.publish(
            "whisper_ready", {"backend": "faster_whisper", "model": SETTINGS.whisper.model, "device": device}
        )
        return backend, state.model


async def transcribe_file(path: str | Path, *, language: str = "sr") -> str:
    backend, model = await _get_model()
    async with _TRANSCRIBE_LOCK:
        loop = asyncio.get_running_loop()
        if backend == "mlx_whisper":
            model_name = SETTINGS.whisper.model

            def _run_mlx() -> str:
                import mlx_whisper  # type: ignore

                result = mlx_whisper.transcribe(
                    str(path),
                    path_or_hf_repo=model_name,
                    language=language,
                )
                return (result.get("text") or "").strip()

            text = await loop.run_in_executor(_EXECUTOR, _run_mlx)
            await BUS.publish(
                "whisper_result", {"backend": "mlx_whisper", "language": language, "text": text}
            )
            return text

        def _run_fw() -> tuple[str, Any]:
            segments, info = model.transcribe(
                str(path),
                language=language,
                vad_filter=True,
                beam_size=1,
                best_of=1,
                condition_on_previous_text=False,
            )
            text = "".join(seg.text for seg in segments).strip()
            return text, info

        text, info = await loop.run_in_executor(_EXECUTOR, _run_fw)
        await BUS.publish(
            "whisper_result",
            {"backend": "faster_whisper", "language": info.language, "duration": info.duration, "text": text},
        )
        return text


async def transcribe_bytes(data: bytes, *, suffix: str = ".webm", language: str = "sr") -> str:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(data)
        tmp = f.name
    try:
        return await transcribe_file(tmp, language=language)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


async def warmup() -> None:
    """Load the model and run one tiny transcription in the background so
    the first real voice message is fast. Best-effort: errors are silent."""
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        rate = 16000
        with wave.open(tmp.name, "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(rate)
            f.writeframes(struct.pack("<h", 0) * (rate // 4))
        try:
            await transcribe_file(tmp.name, language="sr")
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass
