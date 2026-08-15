"""Runtime configuration loader.

Reads `~/.config/kilo/kilo.jsonc` to discover the `bailian-token-plan-personal`
provider (QwenCloud) for the LLM endpoint, then layers environment variables
and `.env` overrides on top. Two providers are supported:

- `minimax` (default): MiniMax API at https://api.minimax.io/v1 (subscription
  token-plan key, prefix `sk-cp-`). Endpoint: POST /chat/completions.
- `bailian`: Bailian/QwenCloud token plan from kilo.jsonc (key prefix `sk-sp-`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Allow `python -m jarvis` from the project root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split("#", 1)[0].strip().strip('"').strip("'")
        out[k.strip()] = v
    return out


def _strip_jsonc(text: str) -> str:
    # Strip // and /* */ comments (the kilo config uses // comments).
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(^|\s)//[^\n]*", r"\1", text)
    return text


def _read_kilo_config() -> dict[str, Any]:
    candidates = [
        Path.home() / ".config" / "kilo" / "kilo.jsonc",
        Path.home() / ".config" / "kilo" / "kilo.json",
        Path.home() / ".kilo" / "config.json",
        Path.home() / ".kilocode" / "config.json",
    ]
    for p in candidates:
        if p.exists():
            try:
                return json.loads(_strip_jsonc(p.read_text(encoding="utf-8")))
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to parse %s: %s", p, exc)
    return {}


@dataclass(frozen=True)
class LLMSettings:
    provider: str  # "minimax" | "bailian"
    base_url: str
    api_key: str
    model: str
    small_model: str
    thinking: str  # "adaptive" | "disabled"
    models: list[dict[str, str]]  # [{id, label}]


@dataclass(frozen=True)
class WhisperSettings:
    backend: str  # "faster_whisper" | "mlx_whisper"
    model: str
    device: str
    compute: str
    hf_endpoint: str


@dataclass(frozen=True)
class PiperSettings:
    voice: str
    length_scale: float
    say_voice: str  # macOS `say` fallback voice (used by `say` backend + Piper fallback)


@dataclass(frozen=True)
class EdgeSettings:
    voice: str  # default edge voice (e.g. sr-RS-NicholasNeural)
    rate: str  # e.g. "+0%"


@dataclass(frozen=True)
class AzureSettings:
    key: str  # JARVIS_AZURE_SPEECH_KEY
    region: str  # JARVIS_AZURE_SPEECH_REGION
    voice: str  # default azure voice


@dataclass(frozen=True)
class ElevenLabsSettings:
    api_key: str  # JARVIS_ELEVENLABS_API_KEY
    voice_id: str  # default voice_id
    model_id: str  # eleven_multilingual_v2 | eleven_turbo_v2_5 | ...
    output_format: str  # mp3_44100_128 | pcm_24000 | ...
    language: str  # ISO-639-1 code ("sr", "en"); empty = auto
    # Voice tuning knobs (ElevenLabs VoiceSettings). All in [0.0, 1.0]
    # unless noted. See https://elevenlabs.io/docs/voice-settings
    stability: float  # 0 = expressive/variable, 1 = stable/monotone
    similarity_boost: float  # 0 = low adherence, 1 = high adherence to voice
    style: float  # 0 = none, 1 = exaggerated (style exaggeration)
    speed: float  # 0.5 .. 2.0 (1.0 = normal)
    use_speaker_boost: bool  # boost clarity of the reference speaker


@dataclass(frozen=True)
class XTTSSettings:
    model: str  # e.g. tts_models/multilingual/multi-dataset/xtts_v2
    speaker_wav: Path  # reference audio for voice cloning (absolute)
    language: str  # language code (sr, en, ...)
    use_gpu: bool


@dataclass(frozen=True)
class TTSSettings:
    backend: str  # "piper" | "say" | "edge" | "azure" | "elevenlabs" | "xtts"
    piper: PiperSettings
    edge: EdgeSettings
    azure: AzureSettings
    elevenlabs: ElevenLabsSettings
    xtts: XTTSSettings


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    open_browser: bool


@dataclass(frozen=True)
class KiloSettings:
    bin: str
    auto: bool
    config_path: Path


@dataclass(frozen=True)
class LocalModelSettings:
    """User-declared catalogue of local (on-device) LLM models.

    Each entry is one Ollama tag the user wants to be able to load/unload
    on demand. The model is NOT loaded at boot — the user loads it from the
    UI when they want to switch to it, and unloads it to free RAM.

    Configured via `JARVIS_LOCAL_MODELS` in `.env` (semicolon-separated
    list of `id|ollama_tag|n_ctx|keep_alive`). Empty by default. Requires
    Ollama running on `localhost:11434` (e.g. `brew services start ollama`).
    """

    entries: list[dict[str, Any]]  # [{id, label, tag, n_ctx, keep_alive}]


@dataclass(frozen=True)
class PushToTalkSettings:
    """Global push-to-talk (radi BILO GDE na macOS-u, ne samo u Jarvis prozoru).

    ``key`` je pynput key spec (e.g. ``"right cmd"``, ``"caps lock"``, ``"f20"``)
    ili macOS chord ``"fn+shift"``. Fn nije običan pynput taster, pa se taj
    chord prati kroz Quartz event tap.

    ``mute_while_held`` mutiraj sistemski zvuk dok je PTT aktivan (default True).
    ``auto_send`` automatski pošalji transkript kao poruku (default True).
    """

    enabled: bool
    key: str
    mute_while_held: bool
    auto_send: bool
    sample_rate: int


@dataclass(frozen=True)
class AudioSettings:
    hotkey: str
    vad_aggressiveness: int
    sample_rate: int
    silence_ms: int
    output: str  # "ui" = browser reprodukuje audio, "say" = server igra afplay
    push_to_talk: PushToTalkSettings


@dataclass(frozen=True)
class Settings:
    root: Path
    llm: LLMSettings
    whisper: WhisperSettings
    piper: PiperSettings
    tts: TTSSettings
    server: ServerSettings
    kilo: KiloSettings
    audio: AudioSettings
    local_models: LocalModelSettings
    permissions_path: Path = field(default_factory=lambda: ROOT / "config" / "permissions.json")
    data_dir: Path = field(default_factory=lambda: ROOT / "data")


def _minimax_from_kilo(kilo_cfg: dict[str, Any]) -> tuple[str, str, list[str], list[str]]:
    """Return (base_url, api_key, models, model_labels) from the kilo config."""
    provider = kilo_cfg.get("provider") or {}
    p = provider.get("bailian-token-plan-personal") or {}
    opts = p.get("options") or {}
    base_url = opts.get("baseURL") or ""
    api_key = opts.get("apiKey") or ""
    models: list[str] = []
    labels: list[str] = []
    for mid, m in (p.get("models") or {}).items():
        models.append(mid)
        labels.append((m.get("name") if isinstance(m, dict) else None) or mid)
    # Also expose the global `model` / `small_model` from the root of kilo config
    # even if they are not enumerated in provider.models (e.g. minimax-coding-plan/*).
    for top in ("model", "small_model"):
        v = kilo_cfg.get(top)
        if v and v not in models:
            models.append(v)
            labels.append(v)
    return base_url, api_key, models, labels


def _env(name: str, dotenv: dict[str, str], default: str | None = None) -> str | None:
    return os.environ.get(name) or dotenv.get(name) or default


def _resolve_path(dotenv: dict[str, str], env_name: str, default: Path) -> Path:
    """Resolve a filesystem path from env/dotenv, anchored to ROOT if relative."""
    raw = _env(env_name, dotenv, str(default)) or str(default)
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = ROOT / p
    return p.resolve()


def _parse_local_models(dotenv: dict[str, str]) -> LocalModelSettings:
    """Parse `JARVIS_LOCAL_MODELS` from .env.

    Format: ``id|ollama_tag|n_ctx|keep_alive|flags`` (``flags`` optional,
    comma-separated: ``notools`` / ``tools`` force the tool-calling
    capability instead of probing). `n_ctx` defaults to 32768 and
    `keep_alive` to `24h` when omitted. This list only OVERRIDES parameters
    of models discovered from Ollama; every installed model is offered
    regardless of whether it appears here.
    """
    raw = _env("JARVIS_LOCAL_MODELS", dotenv, "") or ""
    entries: list[dict[str, Any]] = []
    for chunk in (c.strip() for c in raw.split(";") if c.strip()):
        parts = [p.strip() for p in chunk.split("|")]
        if len(parts) < 2:
            continue
        try:
            n_ctx = int(parts[2]) if len(parts) > 2 and parts[2] else 32768
        except ValueError:
            n_ctx = 32768
        keep_alive = parts[3] if len(parts) > 3 and parts[3] else "24h"
        flags = parts[4] if len(parts) > 4 and parts[4] else ""
        entries.append(
            {
                "id": parts[0],
                "label": parts[0],
                "tag": parts[1],
                "n_ctx": n_ctx,
                "keep_alive": keep_alive,
                "flags": flags,
            }
        )
    return LocalModelSettings(entries=entries)


_MINIMAX_MODELS = [
    {"id": "MiniMax-M3", "label": "MiniMax-M3 (default, reasoning)"},
    {"id": "MiniMax-M2.7", "label": "MiniMax-M2.7"},
    {"id": "MiniMax-M2.7-highspeed", "label": "MiniMax-M2.7 Highspeed (brži)"},
    {"id": "MiniMax-M2.5", "label": "MiniMax-M2.5"},
    {"id": "MiniMax-M2.5-highspeed", "label": "MiniMax-M2.5 Highspeed"},
    {"id": "MiniMax-M2", "label": "MiniMax-M2"},
]

_BAILIAN_MODELS = [
    {"id": "qwen3.8-max", "label": "Qwen3.8 Max (reasoning)"},
    {"id": "qwen3.7-max", "label": "Qwen3.7 Max"},
    {"id": "qwen3.7-plus", "label": "Qwen3.7 Plus"},
    {"id": "qwen3.6-flash", "label": "Qwen3.6 Flash (brži)"},
    {"id": "glm-5.2", "label": "GLM-5.2"},
    {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro"},
    {"id": "deepseek-v4-flash-0731", "label": "DeepSeek V4 Flash"},
]


def _build_llm(dotenv: dict[str, str], kilo_cfg: dict[str, Any]) -> LLMSettings:
    provider = (_env("JARVIS_PROVIDER", dotenv) or "minimax").strip().lower()

    if provider == "bailian":
        base_url, api_key, _models, _labels = _minimax_from_kilo(kilo_cfg)
        return LLMSettings(
            provider="bailian",
            base_url=_env("JARVIS_BAILIAN_BASE_URL", dotenv, base_url) or base_url,
            api_key=_env("JARVIS_BAILIAN_API_KEY", dotenv, api_key) or api_key,
            model=_env("JARVIS_BAILIAN_MODEL", dotenv, "qwen3.8-max") or "qwen3.8-max",
            small_model=_env("JARVIS_BAILIAN_SMALL_MODEL", dotenv, "qwen3.7-plus") or "qwen3.7-plus",
            thinking="disabled",
            models=_BAILIAN_MODELS,
        )

    # default: minimax
    return LLMSettings(
        provider="minimax",
        base_url=_env("JARVIS_MINIMAX_BASE_URL", dotenv, "https://api.minimax.io/v1")
        or "https://api.minimax.io/v1",
        api_key=_env("JARVIS_MINIMAX_API_KEY", dotenv) or "",
        model=_env("JARVIS_MINIMAX_MODEL", dotenv, "MiniMax-M3") or "MiniMax-M3",
        small_model=_env("JARVIS_MINIMAX_SMALL_MODEL", dotenv, "MiniMax-M2.7-highspeed")
        or "MiniMax-M2.7-highspeed",
        thinking=(_env("JARVIS_MINIMAX_THINKING", dotenv, "adaptive") or "adaptive").strip().lower(),
        models=_MINIMAX_MODELS,
    )


def load() -> Settings:
    dotenv = _load_dotenv(ROOT / ".env")
    kilo_cfg = _read_kilo_config()

    llm = _build_llm(dotenv, kilo_cfg)

    if not llm.api_key:
        log.warning(
            "no API key for provider '%s'. Set JARVIS_%s_API_KEY in .env.",
            llm.provider,
            llm.provider.upper(),
        )

    piper = PiperSettings(
        voice=_env("JARVIS_PIPER_VOICE", dotenv, "sr_RS-serbski_institut-medium")
        or "sr_RS-serbski_institut-medium",
        length_scale=float(_env("JARVIS_PIPER_LENGTH_SCALE", dotenv, "1.0") or 1.0),
        say_voice=_env("JARVIS_SAY_VOICE", dotenv, "Lana") or "Lana",
    )

    return Settings(
        root=ROOT,
        llm=llm,
        whisper=WhisperSettings(
            backend=(_env("JARVIS_STT_BACKEND", dotenv, "faster_whisper") or "faster_whisper")
            .strip()
            .lower(),
            model=_env("JARVIS_WHISPER_MODEL", dotenv, "large-v3-turbo") or "large-v3-turbo",
            device=_env("JARVIS_WHISPER_DEVICE", dotenv, "auto") or "auto",
            compute=_env("JARVIS_WHISPER_COMPUTE", dotenv, "int8") or "int8",
            hf_endpoint=_env("HF_ENDPOINT", dotenv, "https://hf-mirror.com") or "https://hf-mirror.com",
        ),
        piper=piper,
        tts=TTSSettings(
            backend=(_env("JARVIS_TTS_BACKEND", dotenv, "edge") or "edge").strip().lower(),
            piper=piper,
            edge=EdgeSettings(
                voice=_env("JARVIS_EDGE_VOICE", dotenv, "sr-RS-NicholasNeural") or "sr-RS-NicholasNeural",
                rate=_env("JARVIS_EDGE_RATE", dotenv, "+0%") or "+0%",
            ),
            azure=AzureSettings(
                key=_env("JARVIS_AZURE_SPEECH_KEY", dotenv, "") or "",
                region=_env("JARVIS_AZURE_SPEECH_REGION", dotenv, "eastasia") or "eastasia",
                voice=_env("JARVIS_AZURE_SPEECH_VOICE", dotenv, "sr-Latn-RS-NicholasNeural")
                or "sr-Latn-RS-NicholasNeural",
            ),
            elevenlabs=ElevenLabsSettings(
                api_key=_env("JARVIS_ELEVENLABS_API_KEY", dotenv, "") or "",
                voice_id=_env("JARVIS_ELEVENLABS_VOICE", dotenv, "sB7vwSCyX0tQmU24cW2C")
                or "sB7vwSCyX0tQmU24cW2C",
                model_id=_env("JARVIS_ELEVENLABS_MODEL", dotenv, "eleven_multilingual_v2")
                or "eleven_multilingual_v2",
                output_format=_env("JARVIS_ELEVENLABS_FORMAT", dotenv, "mp3_44100_128") or "mp3_44100_128",
                # ElevenLabs `eleven_multilingual_v2` does not support "sr" —
                # "hr" is the closest supported ISO code (~95% mutually
                # intelligible with Serbian). Empty string = no language
                # hint (Language Override OFF in the UI).
                language=_env("JARVIS_ELEVENLABS_LANGUAGE", dotenv, "") or "",
                stability=float(_env("JARVIS_ELEVENLABS_STABILITY", dotenv, "0.4") or 0.4),
                similarity_boost=float(_env("JARVIS_ELEVENLABS_SIMILARITY", dotenv, "0.3") or 0.3),
                style=float(_env("JARVIS_ELEVENLABS_STYLE", dotenv, "0.1") or 0.1),
                speed=float(_env("JARVIS_ELEVENLABS_SPEED", dotenv, "1.0") or 1.0),
                use_speaker_boost=(_env("JARVIS_ELEVENLABS_SPEAKER_BOOST", dotenv, "true") or "true").lower()
                in ("1", "true", "yes"),
            ),
            xtts=XTTSSettings(
                model=_env("JARVIS_XTTS_MODEL", dotenv, "tts_models/multilingual/multi-dataset/xtts_v2")
                or "tts_models/multilingual/multi-dataset/xtts_v2",
                speaker_wav=_resolve_path(dotenv, "JARVIS_XTTS_SPEAKER", ROOT / "data" / "xtts_ref.wav"),
                language=_env("JARVIS_XTTS_LANGUAGE", dotenv, "sr") or "sr",
                use_gpu=(_env("JARVIS_XTTS_GPU", dotenv, "true") or "true").lower() in ("1", "true", "yes"),
            ),
        ),
        server=ServerSettings(
            host=_env("JARVIS_HOST", dotenv, "127.0.0.1") or "127.0.0.1",
            port=int(_env("JARVIS_PORT", dotenv, "7777") or 7777),
            open_browser=(_env("JARVIS_OPEN_BROWSER", dotenv, "true") or "true").lower() == "true",
        ),
        kilo=KiloSettings(
            bin=_env("JARVIS_KILO_BIN", dotenv, "kilo") or "kilo",
            auto=(_env("JARVIS_KILO_AUTO", dotenv, "true") or "true").lower() == "true",
            config_path=Path(
                _env("JARVIS_KILO_CONFIG", dotenv, "./config/kilo-jarvis.jsonc")
                or "./config/kilo-jarvis.jsonc"
            ).resolve(),
        ),
        audio=AudioSettings(
            hotkey=_env("JARVIS_HOTKEY", dotenv, "cmd+opt+space") or "cmd+opt+space",
            vad_aggressiveness=int(_env("JARVIS_VAD_AGGRESSIVENESS", dotenv, "2") or 2),
            sample_rate=int(_env("JARVIS_SAMPLE_RATE", dotenv, "16000") or 16000),
            silence_ms=int(_env("JARVIS_SILENCE_MS", dotenv, "900") or 900),
            output=(_env("JARVIS_TTS_OUTPUT", dotenv, "ui") or "ui").strip().lower(),
            push_to_talk=PushToTalkSettings(
                enabled=(_env("JARVIS_PTT_ENABLED", dotenv, "true") or "true").lower()
                in ("1", "true", "yes"),
                key=(_env("JARVIS_PTT_KEY", dotenv, "fn+shift") or "fn+shift").strip().lower(),
                mute_while_held=(_env("JARVIS_PTT_MUTE", dotenv, "true") or "true").lower()
                in ("1", "true", "yes"),
                auto_send=(_env("JARVIS_PTT_AUTO_SEND", dotenv, "true") or "true").lower()
                in ("1", "true", "yes"),
                sample_rate=int(_env("JARVIS_PTT_SAMPLE_RATE", dotenv, "16000") or 16000),
            ),
        ),
        local_models=_parse_local_models(dotenv),
    )


SETTINGS = load()
