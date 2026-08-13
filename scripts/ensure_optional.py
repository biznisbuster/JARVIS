"""Re-run only the optional heavy installs (idempotent)."""

import shutil
import subprocess
import sys
import sysconfig


def have(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=False)


def ensure_pip() -> None:
    run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])


def ensure_mlx_whisper() -> None:
    if have("mlx_whisper"):
        print("mlx-whisper already installed")
        return
    # Apple Silicon only (mlx).
    if sys.platform != "darwin" or sysconfig.get_platform().find("arm64") < 0:
        print("mlx-whisper skipped (needs Apple Silicon)")
        return
    run([sys.executable, "-m", "pip", "install", "mlx-whisper"])


def ensure_tts() -> None:
    if have("TTS"):
        print("Coqui TTS already installed")
        return
    if sys.platform == "darwin":
        if not shutil.which("espeak") and not shutil.which("espeak-ng"):
            print("NOTE: install espeak-ng for some TTS models: brew install espeak-ng")
    run([sys.executable, "-m", "pip", "install", "TTS"])


def ensure_playwright() -> None:
    if have("playwright"):
        print("playwright already installed")
    else:
        run([sys.executable, "-m", "pip", "install", "playwright"])
    # Chromium browser bundle
    cache = Path.home() / ".cache" / "ms-playwright"
    if not any(cache.glob("chromium*")):
        run([sys.executable, "-m", "playwright", "install", "chromium"])


def ensure_rumps() -> None:
    if sys.platform != "darwin":
        print("rumps skipped (macOS only)")
        return
    if have("rumps"):
        print("rumps already installed")
        return
    run([sys.executable, "-m", "pip", "install", "rumps"])


if __name__ == "__main__":
    from pathlib import Path

    ensure_pip()
    ensure_mlx_whisper()
    ensure_tts()
    ensure_playwright()
    ensure_rumps()
    print("All optional installs complete.")
