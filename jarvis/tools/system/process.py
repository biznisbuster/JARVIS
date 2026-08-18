"""Bounded subprocess helpers shared by macOS tool modules."""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any


def _osascript_sync(script: str, timeout: float = 20.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "osascript timeout"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


async def _osascript(script: str, timeout: float = 20.0) -> tuple[int, str, str]:
    return await asyncio.to_thread(_osascript_sync, script, timeout)


def _run_sync(cmd: list[str], timeout: float = 20.0, **kwargs: Any) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kwargs)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


async def _run(cmd: list[str], timeout: float = 20.0, **kwargs: Any) -> tuple[int, str, str]:
    return await asyncio.to_thread(_run_sync, cmd, timeout, **kwargs)
