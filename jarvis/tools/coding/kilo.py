"""Kilo CLI bridge used by the ``kilo_run`` tool."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path

from ...bus import BUS
from ...config import SETTINGS

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


async def _which() -> str | None:
    bin_name = SETTINGS.kilo.bin
    found = shutil.which(bin_name)
    if found:
        return found
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin")):
        candidate = Path(prefix) / bin_name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


async def run_kilo(prompt: str, *, cwd: str | None = None, timeout: float = 180.0) -> str:
    kilo = await _which()
    if not kilo:
        return json.dumps(
            {
                "ok": False,
                "error_code": "DEPENDENCY_MISSING",
                "error": "kilo CLI not found. Install: npm install -g @kilocode/cli",
            },
            ensure_ascii=False,
        )

    cfg = SETTINGS.kilo.config_path
    if not cfg.exists():
        cfg = None

    args = [kilo, "run", "--auto", "--pure", prompt]
    env = os.environ.copy()
    if cfg:
        env["KILO_CONFIG"] = str(cfg)
    env.setdefault("KILO_DISABLE_PROJECT_CONFIG", "1")

    await BUS.publish("kilo_start", {"prompt": prompt[:200], "cwd": cwd, "timeout": timeout})
    started = time.time()

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        stdout_b = b""
        stderr_b = b""
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            await asyncio.sleep(0.3)
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=3)
            except TimeoutError:
                stdout_b, stderr_b = b"", b""
            await BUS.publish(
                "kilo_done",
                {"ok": False, "error": "timeout_force_killed", "elapsed": time.time() - started},
            )
            return json.dumps(
                {
                    "ok": False,
                    "error_code": "TIMEOUT",
                    "error": f"kilo did not exit within {timeout}s; force-killed",
                    "elapsed_s": round(time.time() - started, 1),
                    "output": _clean((stdout_b or b"").decode(errors="replace"))[-2000:],
                },
                ensure_ascii=False,
            )
    except FileNotFoundError as exc:
        return json.dumps(
            {"ok": False, "error_code": "DEPENDENCY_MISSING", "error": str(exc)},
            ensure_ascii=False,
        )

    out = _clean(stdout_b.decode(errors="replace"))
    err = _clean(stderr_b.decode(errors="replace"))
    elapsed = time.time() - started
    await BUS.publish(
        "kilo_done",
        {
            "ok": proc.returncode == 0,
            "exit": proc.returncode,
            "elapsed": elapsed,
            "stdout_bytes": len(stdout_b),
        },
    )

    snippet = (out[-4000:] + ("\n…[stderr]\n" + err[-2000:] if err else "")).strip()
    return json.dumps(
        {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "elapsed_s": round(elapsed, 1),
            "output": snippet,
            "stderr": err[-1000:] if err else "",
        },
        ensure_ascii=False,
    )


async def kilo_run_tool(args: dict[str, object]) -> str:
    """Tool-shaped adapter that preserves the existing max-duration policy."""

    raw_prompt = args.get("prompt")
    prompt = raw_prompt.strip() if isinstance(raw_prompt, str) else ""
    if not prompt:
        return json.dumps(
            {"ok": False, "error_code": "INVALID_ARGUMENTS", "error": "prompt is required"},
            ensure_ascii=False,
        )
    cwd = args.get("cwd")
    max_duration = int(args.get("max_duration_s") or 180)
    return await run_kilo(prompt, cwd=cwd if isinstance(cwd, str) else None, timeout=max_duration)


def _clean(value: str) -> str:
    value = _ANSI.sub("", value)
    return value.replace("\r", "")
