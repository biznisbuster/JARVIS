"""Text-to-Speech with six backends (``SETTINGS.tts.backend``):

* ``say``        - macOS native ``say`` command. Siri-quality neural voices,
  fully offline and free. Apple does not ship a Serbian voice, so the
  default is ``Lana`` (hr_HR) — Croatian is ~95% mutually intelligible with
  Serbian.
* ``edge``       - Microsoft Edge neural voices via `edge-tts`. Best quality
  for Serbian (``sr-RS-NicholasNeural``), free, needs internet.
* ``piper``      - ONNX, runs locally on CPU. Robotic but offline. Falls
  back to ``say`` if Piper is unavailable.
* ``xtts``       - Coqui XTTSv2. Very natural, but does NOT support Serbian
  (17 languages only). Keep for English; use ``say`` or ``edge`` for Serbian.
* ``azure``      - Microsoft Azure Speech REST API. Same neural voices as
  Edge, reachable from networks where ``edge-tts`` is blocked.
* ``elevenlabs`` - ElevenLabs cloud TTS. Highest quality multilingual
  voices. Requires API key.

All synthesis writes into ``data/tts/`` so files can be served to the UI
over HTTP. The server-side speech scheduler (`jarvis/audio/speech.py`) is
the only caller in the normal chat flow; the REST endpoints remain for
voice testing and manual replay.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .. import state as runtime_state
from ..bus import BUS
from ..config import SETTINGS

log = logging.getLogger(__name__)

_BACKENDS = ("say", "edge", "piper", "xtts", "azure", "elevenlabs")
_OUTPUT_SUFFIX = {"edge": ".mp3", "azure": ".mp3", "elevenlabs": ".mp3"}
_DEFAULT_OUTPUT_SUFFIX = ".wav"

_VOICE_DIR = Path.home() / ".local" / "share" / "piper" / "voices"
_XTTS_BOOTSTRAP_DIR = SETTINGS.root / "data" / ".xtts_bootstrap"

_TTS_DIR = SETTINGS.data_dir / "tts"
_MAX_CACHED_FILES = 80


def _synth_timeout() -> float:
    try:
        return max(float(os.environ.get("JARVIS_TTS_SYNTH_TIMEOUT", "20")), 1.0)
    except ValueError:
        return 20.0


def tts_dir() -> Path:
    _TTS_DIR.mkdir(parents=True, exist_ok=True)
    return _TTS_DIR


def _gc_tts_dir() -> None:
    try:
        files = sorted(
            (p for p in _TTS_DIR.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        for p in files[:-_MAX_CACHED_FILES]:
            p.unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Runtime overrides
#
# The user can switch backend/voice from the UI without restarting. One dict
# is the single place to read or reset that state; the frozen ``SETTINGS``
# holds the disk-persisted defaults.
# --------------------------------------------------------------------------


_RUNTIME: dict[str, str | None] = {
    "backend": None,
    "say_voice": None,
    "edge_voice": None,
    "azure_voice": None,
    "elevenlabs_voice": None,
}


def _env_path() -> Path:
    return SETTINGS.root / ".env"


def _dotenv_read(key: str) -> str:
    path = _env_path()
    if not path.exists():
        return ""
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.split("#", 1)[0].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _dotenv_write(key: str, value: str) -> None:
    path = _env_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except OSError:
        lines = []
    for i, line in enumerate(lines):
        if line.strip().startswith(key + "="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Voice resolution (single source of truth)
# --------------------------------------------------------------------------


def _backend() -> str:
    be = (_RUNTIME["backend"] or SETTINGS.tts.backend).strip().lower()
    if be not in _BACKENDS:
        return "edge"
    return be


def _say_voice() -> str:
    return _RUNTIME["say_voice"] or SETTINGS.tts.piper.say_voice or "Lana"


def _edge_voice() -> str:
    return _RUNTIME["edge_voice"] or SETTINGS.tts.edge.voice


def _edge_rate() -> str:
    return SETTINGS.tts.edge.rate or "+0%"


def _azure_voice() -> str:
    return _RUNTIME["azure_voice"] or SETTINGS.tts.azure.voice


def _azure_available() -> bool:
    return bool(SETTINGS.tts.azure.key) or bool(os.environ.get("JARVIS_AZURE_SPEECH_KEY", ""))


def _elevenlabs_voice() -> str:
    return _RUNTIME["elevenlabs_voice"] or SETTINGS.tts.elevenlabs.voice_id


def _elevenlabs_available() -> bool:
    return bool(SETTINGS.tts.elevenlabs.api_key) or bool(os.environ.get("ELEVENLABS_API_KEY", ""))


def _active_voice(backend: str) -> str:
    if backend == "say":
        return _say_voice()
    if backend == "edge":
        return _edge_voice()
    if backend == "azure":
        return _azure_voice()
    if backend == "elevenlabs":
        return _elevenlabs_voice()
    if backend == "piper":
        return SETTINGS.tts.piper.voice
    return SETTINGS.tts.xtts.model


def current_voice_info() -> dict[str, str]:
    be = _backend()
    return {"backend": be, "voice": _active_voice(be)}


def set_voice(backend: str, voice: str | None = None) -> dict[str, str]:
    """Switch TTS engine and/or voice at runtime. Persists to ``.env`` so
    the choice survives a restart. Returns the effective (backend, voice)."""
    be = (backend or SETTINGS.tts.backend).strip().lower()
    if be not in _BACKENDS:
        raise ValueError(f"unknown TTS backend: {backend!r}")

    _RUNTIME["backend"] = be
    _dotenv_write("JARVIS_TTS_BACKEND", be)

    if be == "say" and voice:
        _RUNTIME["say_voice"] = voice
        _dotenv_write("JARVIS_SAY_VOICE", voice)
    elif be == "edge" and voice:
        _RUNTIME["edge_voice"] = voice
        _dotenv_write("JARVIS_EDGE_VOICE", voice)
    elif be == "azure":
        if not _azure_available():
            raise ValueError("Azure Speech key not set (JARVIS_AZURE_SPEECH_KEY)")
        if voice:
            _RUNTIME["azure_voice"] = voice
            _dotenv_write("JARVIS_AZURE_SPEECH_VOICE", voice)
    elif be == "elevenlabs":
        if not _elevenlabs_available():
            raise ValueError("ElevenLabs API key not set (JARVIS_ELEVENLABS_API_KEY)")
        if voice:
            _RUNTIME["elevenlabs_voice"] = voice
            _dotenv_write("JARVIS_ELEVENLABS_VOICE", voice)

    return current_voice_info()


# --------------------------------------------------------------------------
# Voice catalogues (used by the UI selector)
# --------------------------------------------------------------------------


_BALKAN_LOCALES = (
    "sr_",
    "hr_",
    "bs_",
    "mk_",
    "sl_",
    "sq_",
    "bg_",
    "ro_",
    "el_",
    "al_",
    "sr-",
    "hr-",
    "bs-",
    "mk-",
    "sl-",
    "bg-",
    "ro-",
)


def list_say_voices() -> list[dict[str, str]]:
    """Available macOS ``say`` voices, filtered to Balkan/Slavic locales."""
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001
        return []
    voices: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        head = line.split("#", 1)[0].strip()
        if not head:
            continue
        parts = head.rsplit(None, 1)
        name = parts[0] if parts else ""
        locale = parts[-1] if len(parts) > 1 else ""
        if not name or name in seen:
            continue
        if not any(locale.startswith(p) for p in _BALKAN_LOCALES):
            continue
        seen.add(name)
        voices.append({"id": name, "label": f"{name} ({locale})", "locale": locale})
    return voices


_EDGE_VOICES: list[dict[str, str]] = [
    {"id": "sr-RS-NicholasNeural", "label": "sr-RS Nicholas (muški)", "locale": "sr-RS"},
    {"id": "sr-RS-SophieNeural", "label": "sr-RS Sophie (ženski)", "locale": "sr-RS"},
]


_AZURE_VOICES: list[dict[str, str]] = [
    {"id": "sr-Latn-RS-NicholasNeural", "label": "sr-Latn Nicholas (muški)", "locale": "sr-RS"},
    {"id": "sr-Latn-RS-SophieNeural", "label": "sr-Latn Sophie (ženski)", "locale": "sr-RS"},
    {"id": "sr-RS-NicholasNeural", "label": "sr-RS Nicholas (ćirilica)", "locale": "sr-RS"},
    {"id": "sr-RS-SophieNeural", "label": "sr-RS Sophie (ćirilica)", "locale": "sr-RS"},
]


_ELEVENLABS_VOICES: list[dict[str, str]] = [
    {"id": "JBFqnCBsd6RMkjVDRZzb", "label": "George — pripovedač (muški) · free", "locale": "multilingual"},
    {"id": "ErXwobaYiN019PkySvjV", "label": "Antoni — topao (muški) · free", "locale": "multilingual"},
    {"id": "pNInz6obpgDQGcFmaJgB", "label": "Adam — naracija (muški) · free", "locale": "multilingual"},
    {"id": "EXAVITQu4vr4xnSDxMaL", "label": "Bella — nežna (ženski) · free", "locale": "multilingual"},
    {
        "id": "sB7vwSCyX0tQmU24cW2C",
        "label": "Jon — Natural Authority (muški) · paid",
        "locale": "multilingual",
    },
]


def list_edge_voices() -> list[dict[str, str]]:
    return list(_EDGE_VOICES)


def list_azure_voices() -> list[dict[str, str]]:
    return list(_AZURE_VOICES)


def list_elevenlabs_voices() -> list[dict[str, str]]:
    return list(_ELEVENLABS_VOICES)


def list_voices() -> dict[str, list[dict[str, str]]]:
    """Voice catalogue for every backend. The UI shows an ``optgroup`` per
    non-empty list."""
    return {
        "say": list_say_voices(),
        "edge": list_edge_voices(),
        "azure": list_azure_voices() if _azure_available() else [],
        "elevenlabs": list_elevenlabs_voices() if _elevenlabs_available() else [],
        "piper": [
            {"id": SETTINGS.tts.piper.voice, "label": f"Piper {SETTINGS.tts.piper.voice}", "locale": "sr"}
        ],
        "xtts": [
            {
                "id": SETTINGS.tts.xtts.model,
                "label": f"XTTS ({SETTINGS.tts.xtts.language})",
                "locale": SETTINGS.tts.xtts.language,
            }
        ],
    }


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _ffmpeg_convert(src: Path, dst: Path, *, rate: int | None = None, mono: bool = True) -> bool:
    ff = shutil.which("ffmpeg")
    if not ff:
        return False
    cmd = [ff, "-y", "-i", str(src)]
    if mono:
        cmd += ["-ac", "1"]
    if rate:
        cmd += ["-ar", str(rate)]
    cmd += ["-acodec", "pcm_s16le", str(dst)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode == 0


async def _emit_done(engine: str, **payload: Any) -> None:
    payload_full = {"engine": engine, "path": str(payload.pop("path", "")), **payload}
    await BUS.publish("tts_done", payload_full)


# --------------------------------------------------------------------------
# macOS `say` backend (Siri neural voices, offline, free)
# --------------------------------------------------------------------------


async def _synth_say(text: str, out_path: Path) -> None:
    voice = _say_voice()
    aiff = out_path.with_suffix(".aiff")
    cmd = ["say", "-o", str(aiff), "-v", voice, text]
    loop = asyncio.get_running_loop()

    def _run() -> None:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"say failed: {proc.stderr.strip() or proc.stdout.strip()}")
        if _ffmpeg_convert(aiff, out_path):
            aiff.unlink(missing_ok=True)
        else:
            shutil.move(str(aiff), str(out_path))

    await loop.run_in_executor(None, _run)
    await _emit_done("say", voice=voice, text_len=len(text), path=out_path)


# --------------------------------------------------------------------------
# Piper backend (ONNX, CPU) with `say` fallback
# --------------------------------------------------------------------------


async def _ensure_piper_voice() -> tuple[Path, Path] | None:
    voice = SETTINGS.tts.piper.voice
    onnx = _VOICE_DIR / f"{voice}.onnx"
    cfg = _VOICE_DIR / f"{voice}.onnx.json"
    if onnx.exists() and cfg.exists():
        return onnx, cfg

    _VOICE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from piper.download_voices import download_voice

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, download_voice, voice, str(_VOICE_DIR))
    except Exception as exc:  # noqa: BLE001
        await BUS.publish("piper_unavailable", {"reason": f"voice download failed: {exc}"})
        return None

    if onnx.exists() and cfg.exists():
        return onnx, cfg
    await BUS.publish("piper_unavailable", {"reason": f"voice not found after download: {voice}"})
    return None


async def _get_piper() -> Any:
    state = runtime_state.piper_state
    if state.model is not None:
        return state.model
    voice_paths = await _ensure_piper_voice()
    if voice_paths is None:
        return None
    from piper import PiperVoice  # type: ignore

    onnx, cfg = voice_paths

    def _load() -> Any:
        return PiperVoice.load(str(onnx), str(cfg), use_cuda=False)

    loop = asyncio.get_running_loop()
    state.model = await loop.run_in_executor(None, _load)
    await BUS.publish("piper_ready", {"voice": SETTINGS.tts.piper.voice})
    return state.model


async def _synth_piper(text: str, out_path: Path) -> None:
    piper = await _get_piper()
    if piper is None:
        await _synth_say(text, out_path)
        return

    loop = asyncio.get_running_loop()

    def _run() -> None:
        import wave

        from piper.config import SynthesisConfig

        cfg = SynthesisConfig(length_scale=SETTINGS.tts.piper.length_scale)
        with wave.open(str(out_path), "wb") as f:
            piper.synthesize_wav(text, f, syn_config=cfg, set_wav_format=True)

    await loop.run_in_executor(None, _run)
    await _emit_done("piper", voice=SETTINGS.tts.piper.voice, text_len=len(text), path=out_path)


# --------------------------------------------------------------------------
# Azure Speech REST backend (Microsoft neural voices, direct REST API)
# --------------------------------------------------------------------------


def _azure_key() -> str:
    return (
        os.environ.get("JARVIS_AZURE_SPEECH_KEY", "")
        or SETTINGS.tts.azure.key
        or _dotenv_read("JARVIS_AZURE_SPEECH_KEY")
    )


def _azure_region() -> str:
    return os.environ.get("JARVIS_AZURE_SPEECH_REGION", "") or SETTINGS.tts.azure.region


def _escape_ssml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


async def _synth_azure(text: str, out_path: Path) -> None:
    import httpx

    key = _azure_key()
    if not key:
        raise RuntimeError("Azure TTS: JARVIS_AZURE_SPEECH_KEY not set")
    region = _azure_region()
    voice = _azure_voice()
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    ssml = (
        '<speak version="1.0" xml:lang="sr-Latn-RS">'
        f'<voice name="{voice}">{_escape_ssml(text)}</voice></speak>'
    )
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
        "User-Agent": "Jarvis",
    }
    loop = asyncio.get_running_loop()

    def _run() -> None:
        resp = httpx.post(url, headers=headers, content=ssml.encode("utf-8"), timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"Azure TTS HTTP {resp.status_code}: {resp.text[:300]}")
        out_path.write_bytes(resp.content)

    await loop.run_in_executor(None, _run)
    await _emit_done("azure", voice=voice, text_len=len(text), path=out_path)


# --------------------------------------------------------------------------
# Edge TTS backend (Microsoft neural voices, best Serbian quality)
# --------------------------------------------------------------------------


async def _synth_edge(text: str, out_path: Path) -> None:
    import edge_tts  # type: ignore

    voice = _edge_voice()
    rate = _edge_rate()
    loop = asyncio.get_running_loop()

    def _run() -> None:
        import asyncio as _asyncio

        async def _gen() -> None:
            comm = edge_tts.Communicate(text, voice=voice, rate=rate)
            await comm.save(str(out_path))

        _asyncio.run(_gen())

    await loop.run_in_executor(None, _run)
    await _emit_done("edge", voice=voice, text_len=len(text), path=out_path)


# --------------------------------------------------------------------------
# ElevenLabs backend (premium multilingual neural voices)
# --------------------------------------------------------------------------


async def _synth_elevenlabs(text: str, out_path: Path) -> None:
    api_key = os.environ.get("ELEVENLABS_API_KEY", "") or SETTINGS.tts.elevenlabs.api_key
    if not api_key:
        raise RuntimeError("ElevenLabs TTS: JARVIS_ELEVENLABS_API_KEY not set")
    from elevenlabs.client import ElevenLabs
    from elevenlabs.core.api_error import ApiError
    from elevenlabs.types.voice_settings import VoiceSettings

    cfg = SETTINGS.tts.elevenlabs
    voice_id = _elevenlabs_voice()
    model_id = cfg.model_id
    output_format = cfg.output_format
    language = cfg.language or None
    voice_settings = VoiceSettings(
        stability=cfg.stability,
        similarity_boost=cfg.similarity_boost,
        style=cfg.style,
        speed=cfg.speed,
        use_speaker_boost=cfg.use_speaker_boost,
    )

    def _run() -> None:
        client = ElevenLabs(api_key=api_key)
        kwargs: dict[str, Any] = {"voice_settings": voice_settings}
        if language:
            kwargs["language_code"] = language
        audio_iter = client.text_to_speech.convert(
            voice_id=voice_id,
            text=text,
            model_id=model_id,
            output_format=output_format,
            **kwargs,
        )
        with open(out_path, "wb") as f:
            for chunk in audio_iter:
                if chunk:
                    f.write(chunk)

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _run)
    except ApiError as exc:
        status = getattr(exc, "status_code", "?")
        msg = ""
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            detail = body.get("detail") or {}
            msg = detail.get("message") or body.get("message") or ""
        if status == 402:
            hint = f"ElevenLabs zahteva plaćeni plan za voice {voice_id}. Izaberi free glas (George/Antoni/Adam/Bella) u UI."
        elif status == 404:
            hint = f"Voice {voice_id} ne postoji ili nije tvoj."
        elif status == 401:
            hint = "ElevenLabs API ključ nevažeći."
        elif status == 400:
            hint = f"Loš parametar: {msg}"
        else:
            hint = f"ElevenLabs {status}: {msg}"
        await BUS.publish("tts_error", {"engine": "elevenlabs", "voice": voice_id, "error": hint})
        raise RuntimeError(hint) from exc
    await _emit_done(
        "elevenlabs",
        voice=voice_id,
        model=model_id,
        format=output_format,
        text_len=len(text),
        path=out_path,
    )


# --------------------------------------------------------------------------
# XTTSv2 backend (Coqui TTS)
# --------------------------------------------------------------------------


async def _ensure_xtts_reference() -> Path:
    """Make sure a reference audio exists for XTTS voice cloning."""
    ref = SETTINGS.tts.xtts.speaker_wav
    if ref.exists() and ref.stat().st_size > 1024:
        return ref

    _XTTS_BOOTSTRAP_DIR.mkdir(parents=True, exist_ok=True)
    await BUS.publish("xtts_bootstrap", {"reason": "missing reference", "path": str(ref)})

    sample_text = (
        "Dobar dan, ja sam Jarvis, vaš lični glasovni asistent. "
        "Danas je divan dan za rad i učenje novih stvari. "
        "Mogu da vam pomognem sa pretragom informacija i zapisivanjem beleški. "
        "Sve što vam treba, samo pitajte i ja ću vam rado pomoći."
    )

    boot_mp3 = _XTTS_BOOTSTRAP_DIR / "sample.mp3"
    boot_wav = _XTTS_BOOTSTRAP_DIR / "sample.wav"
    loop = asyncio.get_running_loop()

    def _gen() -> None:
        generated = False
        try:
            import asyncio as _asyncio

            import edge_tts

            async def _run_edge() -> None:
                comm = edge_tts.Communicate(sample_text, voice=_edge_voice())
                await comm.save(str(boot_mp3))

            _asyncio.run(_run_edge())
            if _ffmpeg_convert(boot_mp3, boot_wav, rate=24000, mono=True):
                generated = True
        except Exception as exc:  # noqa: BLE001
            log.warning("edge ref failed (%s), trying say", exc)

        if not generated:
            aiff = boot_wav.with_suffix(".aiff")
            subprocess.run(["say", "-o", str(aiff), "-v", _say_voice(), sample_text], check=True)
            if not _ffmpeg_convert(aiff, boot_wav, rate=24000, mono=True):
                shutil.move(str(aiff), str(boot_wav))

        trimmed = _XTTS_BOOTSTRAP_DIR / "trimmed.wav"
        if _ffmpeg_convert(boot_wav, trimmed, rate=24000, mono=True):
            shutil.move(str(trimmed), str(ref))
        else:
            shutil.move(str(boot_wav), str(ref))

    await loop.run_in_executor(None, _gen)
    await BUS.publish("xtts_ready", {"path": str(ref), "note": "generated from Edge/Say sample"})
    return ref


async def _get_xtts() -> Any:
    state = runtime_state.xtts_state
    if state.model is not None:
        return state.model

    os.environ.setdefault("COQUI_TOS_AGREED", "1")

    try:
        import torch

        _orig_load = torch.load

        def _legacy_load(*args: Any, **kwargs: Any) -> Any:
            kwargs.setdefault("weights_only", False)
            return _orig_load(*args, **kwargs)

        torch.load = _legacy_load  # type: ignore[assignment]
    except Exception:  # noqa: BLE001
        pass

    await _ensure_xtts_reference()
    loop = asyncio.get_running_loop()

    def _load() -> Any:
        from TTS.api import TTS as CoquiTTS  # type: ignore

        if SETTINGS.tts.xtts.use_gpu:
            try:
                import torch

                if torch.backends.mps.is_available():
                    return CoquiTTS(SETTINGS.tts.xtts.model).to("mps")
            except Exception:
                pass
        return CoquiTTS(SETTINGS.tts.xtts.model)

    state.model = await loop.run_in_executor(None, _load)
    await BUS.publish("xtts_ready", {"model": SETTINGS.tts.xtts.model, "device": "mps"})
    return state.model


async def _synth_xtts(text: str, out_path: Path) -> None:
    tts = await _get_xtts()
    loop = asyncio.get_running_loop()

    def _run() -> None:
        tts.tts_to_file(
            text=text,
            file_path=str(out_path),
            speaker_wav=str(SETTINGS.tts.xtts.speaker_wav),
            language=SETTINGS.tts.xtts.language,
            split_sentences=True,
        )

    await loop.run_in_executor(None, _run)
    await _emit_done(
        "xtts",
        model=SETTINGS.tts.xtts.model,
        language=SETTINGS.tts.xtts.language,
        text_len=len(text),
        path=out_path,
    )


# --------------------------------------------------------------------------
# Public dispatch
# --------------------------------------------------------------------------


_SYNTHESIZERS: dict[str, Callable[[str, Path], Awaitable[None]]] = {
    "edge": _synth_edge,
    "azure": _synth_azure,
    "elevenlabs": _synth_elevenlabs,
    "say": _synth_say,
    "xtts": _synth_xtts,
    "piper": _synth_piper,
}


async def synthesize(text: str) -> str:
    """Synthesize ``text`` with the active backend into ``data/tts/`` and
    return the file path. Never refuses on language — mixed Serbian/English
    answers (code, tech terms) must still be spoken.

    Robustness: each synthesis attempt is bounded by a timeout, and any
    non-``say`` backend failure (error or hang) falls back to ``say``, which
    is always available on macOS. Only if ``say`` fails too does the error
    propagate (the speech scheduler turns it into a ``tts_error`` event).
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("empty text")

    backend = _backend()
    suffix = _OUTPUT_SUFFIX.get(backend, _DEFAULT_OUTPUT_SUFFIX)
    out_path = tts_dir() / f"{uuid.uuid4().hex}{suffix}"
    _gc_tts_dir()

    synth = _SYNTHESIZERS.get(backend)
    if synth is None:
        raise ValueError(f"unknown TTS backend: {backend!r}")
    timeout = _synth_timeout()
    try:
        await asyncio.wait_for(synth(text, out_path), timeout=timeout)
    except asyncio.CancelledError:
        out_path.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        out_path.unlink(missing_ok=True)
        if backend == "say":
            raise
        await BUS.publish(
            "tts_fallback",
            {"engine": backend, "fallback": "say", "error": str(exc)[:200]},
        )
        try:
            await asyncio.wait_for(_synth_say(text, out_path), timeout=timeout)
        except BaseException:
            out_path.unlink(missing_ok=True)
            raise
    return str(out_path)
