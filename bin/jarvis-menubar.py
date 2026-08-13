"""macOS menu-bar launcher (rumps).

Provides a tray icon with quick actions: open the control panel, restart the
server, run doctor, quit. The icon only shows up while the Python process is
running; quitting this process removes it.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    import rumps  # type: ignore
except Exception as exc:  # noqa: BLE001
    print(f"[jarvis-menubar] rumps unavailable: {exc}", file=sys.stderr)
    print("Install with: pip install rumps", file=sys.stderr)
    sys.exit(0)


HOST = os.environ.get("JARVIS_HOST", "127.0.0.1")
PORT = int(os.environ.get("JARVIS_PORT", "7777"))
URL = f"http://{HOST}:{PORT}/"


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _start_server() -> None:
    env = os.environ.copy()
    proc = subprocess.Popen(
        [sys.executable, "-m", "jarvis", "serve", "--no-browser"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Give it a moment, then verify.
    for _ in range(40):
        if _port_open(HOST, PORT):
            return
        time.sleep(0.25)
    print(f"[jarvis-menubar] server did not start; pid={proc.pid}", file=sys.stderr)


class JarvisApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Jarvis", title="✦")
        self.menu = [
            rumps.MenuItem("Otvori kontrolni panel", callback=self.open_ui),
            None,
            rumps.MenuItem("Restart servera", callback=self.restart),
            rumps.MenuItem("Status / doktor", callback=self.doctor),
            None,
            rumps.MenuItem("Zatvori Jarvis", callback=self.quit_app),
        ]
        threading.Thread(target=_start_server, daemon=True).start()

    def open_ui(self, _sender: rumps.MenuItem) -> None:
        if not _port_open(HOST, PORT):
            rumps.notification("Jarvis", "Server nije pokrenut", "Pokreni kroz 'Restart servera'")
            return
        webbrowser.open(URL)

    def restart(self, _sender: rumps.MenuItem) -> None:
        # `jarvis stop` šalje SIGTERM procesu na portu i čeka da se port
        # zatvori (verifikovano u Fazi 5); tek onda kreće novi server.
        subprocess.run(
            [sys.executable, "-m", "jarvis", "stop"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        threading.Thread(target=_start_server, daemon=True).start()
        rumps.notification("Jarvis", "Restartovan", f"http://{HOST}:{PORT}")

    def doctor(self, _sender: rumps.MenuItem) -> None:
        out = subprocess.run(
            [sys.executable, "-m", "jarvis", "doctor"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        text = out.stdout or out.stderr
        rumps.window("Jarvis — doktor", text, default_text=text).run()

    def quit_app(self, _sender: rumps.MenuItem | None = None) -> None:
        rumps.quit_application()


if __name__ == "__main__":
    JarvisApp().run()
