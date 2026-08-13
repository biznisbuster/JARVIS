"""Tiny terminal launcher used by `python -m jarvis`.

Subcommands:
  serve      start the FastAPI server (default)
  ui         open the control panel in the default browser
  stop       send SIGTERM to a running server (if any)
  doctor     check connections + tooling
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import webbrowser

from .config import SETTINGS


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def cmd_serve(args: argparse.Namespace) -> int:
    host = args.host or SETTINGS.server.host
    port = args.port or SETTINGS.server.port

    if _port_open(host, port):
        print(f"[jarvis] port {port} already in use; reusing running instance")
        if not args.no_browser:
            webbrowser.open(f"http://{host}:{port}/")
        return 0

    if not args.no_browser and SETTINGS.server.open_browser and "--no-browser" not in sys.argv:

        def _open_later() -> None:
            time.sleep(1.2)
            try:
                webbrowser.open(f"http://{host}:{port}/")
            except Exception:
                pass

        import threading

        threading.Thread(target=_open_later, daemon=True).start()

    import uvicorn

    uvicorn.run(
        "jarvis.app:app",
        host=host,
        port=port,
        reload=args.reload,
        log_level="info",
        access_log=False,
    )
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    host = args.host or SETTINGS.server.host
    port = args.port or SETTINGS.server.port
    url = f"http://{host}:{port}/"
    if not _port_open(host, port):
        print(f"[jarvis] server not running on {host}:{port}. Start with: jarvis serve")
        return 1
    webbrowser.open(url)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    host = args.host or SETTINGS.server.host
    port = args.port or SETTINGS.server.port
    if not _port_open(host, port):
        print(f"[jarvis] nema pokrenutog servera na {host}:{port}")
        return 1
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pids = [int(p) for p in out.stdout.split() if p.strip().isdigit()]
    except (OSError, subprocess.SubprocessError, ValueError):
        pids = []
    if not pids:
        print(f"[jarvis] port {port} je otvoren, ali ne mogu da nađem PID (lsof)")
        return 1
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    print(f"[jarvis] SIGTERM poslat: pid {', '.join(map(str, pids))}")
    for _ in range(30):
        if not _port_open(host, port):
            print("[jarvis] server zaustavljen")
            return 0
        time.sleep(0.2)
    print(f"[jarvis] port {port} je i dalje otvoren posle SIGTERM-a")
    return 1


def _ollama_reachable() -> str:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as resp:
            return f"da (HTTP {resp.status})"
    except (urllib.error.URLError, OSError):
        return "ne"


def cmd_doctor(args: argparse.Namespace) -> int:
    import json

    host = SETTINGS.server.host
    port = SETTINGS.server.port
    print("=== environment ===")
    print(f"python         : {sys.version.split()[0]}  ({sys.executable})")
    print(f"ffmpeg         : {shutil.which('ffmpeg') or 'missing'}")
    print(f"osascript      : {shutil.which('osascript') or 'missing'}")
    print(f"nowplaying-cli : {shutil.which('nowplaying-cli') or 'NOT FOUND'}")
    print(f"ollama bin     : {shutil.which('ollama') or 'missing'}")
    print(f"ollama daemon  : {_ollama_reachable()}")
    print(f"kilo CLI       : {shutil.which(SETTINGS.kilo.bin) or 'NOT FOUND'}")
    print()
    print("=== server ===")
    print(f"port {port:<9}: {'pokrenut' if _port_open(host, port) else 'nije pokrenut'}")
    data_dir = SETTINGS.data_dir
    if data_dir.exists():
        data_state = "upisiv" if os.access(data_dir, os.W_OK) else "NIJE upisiv"
    else:
        data_state = "ne postoji (kreira se pri prvom startu)"
    print(f"data dir       : {data_dir} ({data_state})")
    print()
    print("=== LLM provider ===")
    print(f"provider     : {SETTINGS.llm.provider}")
    print(f"base_url     : {SETTINGS.llm.base_url}")
    print(f"model        : {SETTINGS.llm.model}")
    print(f"api_key set  : {bool(SETTINGS.llm.api_key)}")
    print()
    print("=== audio ===")
    print(f"whisper model: {SETTINGS.whisper.model}")
    print(f"piper voice  : {SETTINGS.piper.voice}")
    print()
    print("=== permissions ===")
    if SETTINGS.permissions_path.exists():
        try:
            cfg = json.loads(SETTINGS.permissions_path.read_text())
        except ValueError:
            print(f"FAJL POSTOJI ALI JE NEVAŽEĆI JSON: {SETTINGS.permissions_path}")
            return 1
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
    else:
        print(f"permissions fajl NE POSTOJI: {SETTINGS.permissions_path} (kreira se pri prvom startu)")
    return 0


def main(argv: list[str] | None = None) -> int:
    from .log import setup_logging

    setup_logging()
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis — lični AI asistent")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="pokreni Jarvis backend (FastAPI)")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", default=None, type=int)
    p_serve.add_argument("--reload", action="store_true", help="autoreload (development)")
    p_serve.add_argument("--no-browser", action="store_true")

    p_ui = sub.add_parser("ui", help="otvori kontrolni panel u browseru")
    p_ui.add_argument("--host", default=None)
    p_ui.add_argument("--port", default=None, type=int)

    p_stop = sub.add_parser("stop", help="zaustavi pokrenut server (SIGTERM po portu)")
    p_stop.add_argument("--host", default=None)
    p_stop.add_argument("--port", default=None, type=int)

    sub.add_parser("doctor", help="prikaži status konekcija i alata")

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        return cmd_serve(args)
    if args.cmd == "ui":
        return cmd_ui(args)
    if args.cmd == "stop":
        return cmd_stop(args)
    if args.cmd == "doctor":
        return cmd_doctor(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
