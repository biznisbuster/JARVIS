#!/usr/bin/env python3
"""Fix XTTSv2 setup: ensure TOS is auto-agreed, download the model, and
create a clean human-like reference audio for voice cloning.

The reference matters a lot: XTTS clones the *timbre* of the reference, so a
robotic Piper sample produces a robotic cloned voice. We try to fetch a short
clean human speech clip (public domain-ish) and fall back to Piper otherwise.
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

os.environ.setdefault("COQUI_TOS_AGREED", "1")

ROOT = Path(__file__).resolve().parent.parent
REF = ROOT / "data" / "xtts_ref.wav"


def ensure_model() -> str:
    from TTS.utils.manage import ModelManager

    m = ModelManager()
    _item, full, _model, _md5 = m.download_model("tts_models/multilingual/multi-dataset/xtts_v2")
    # Ensure the TOS marker exists at the actual model path.
    marker = Path(full) / "tos_agreed.txt"
    marker.write_text("I have read, understood and agreed to the Terms and Conditions.")
    print(f"model: {full}")
    return full


def fetch_reference() -> Path | None:
    """Try to grab a clean 5-6s human speech clip for voice cloning."""
    candidates = [
        # Coqui sample (English, clean female/male). Good timbre for XTTS.
        "https://huggingface.co/coqui/XTTS-v2/resolve/main/samples/female.wav",
        "https://huggingface.co/coqui/XTTS-v2/resolve/main/samples/male.wav",
    ]
    for url in candidates:
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            print(f"fetching {url.split('/')[-1]} ...")
            urllib.request.urlretrieve(url, tmp.name)  # noqa: S310
            if Path(tmp.name).stat().st_size > 10_000:
                return Path(tmp.name)
            Path(tmp.name).unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  failed: {exc}")
    return None


def trim_to(ref_src: Path, out: Path, seconds: float = 6.0) -> bool:
    ff = shutil.which("ffmpeg")
    if not ff:
        shutil.copy(ref_src, out)
        return True
    r = subprocess.run(
        [
            ff,
            "-y",
            "-i",
            str(ref_src),
            "-t",
            str(seconds),
            "-acodec",
            "pcm_s16le",
            "-ar",
            "24000",
            "-ac",
            "1",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0 and out.exists() and out.stat().st_size > 10_000


def make_reference() -> None:
    REF.parent.mkdir(parents=True, exist_ok=True)
    if REF.exists() and REF.stat().st_size > 10_000:
        print(f"reference already exists: {REF}")
        return
    src = fetch_reference()
    if src and trim_to(src, REF):
        print(f"reference: {REF} (human sample)")
        src.unlink(missing_ok=True)
        return
    if src:
        src.unlink(missing_ok=True)
    print("no human sample — generating Piper reference (robotic timbre)")
    asyncio.run(_piper_ref())


async def _piper_ref() -> None:
    from jarvis.audio.tts import _get_piper  # type: ignore

    piper = await _get_piper()
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    import wave

    with wave.open(tmp.name, "wb") as f:
        piper.synthesize_wav(
            "Zdravo, ja sam Jarvis. Ovo je moj glas.", f, syn_config=None, set_wav_format=True
        )
    trim_to(Path(tmp.name), REF)
    Path(tmp.name).unlink(missing_ok=True)
    print(f"reference: {REF} (Piper fallback)")


def main() -> int:
    try:
        ensure_model()
        make_reference()
        print("XTTS setup complete.")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
