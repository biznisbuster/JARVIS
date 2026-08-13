"""Structured logging setup for the whole app.

Level is controlled via ``JARVIS_LOG_LEVEL`` (default ``INFO``). Call
``setup_logging()`` once at startup; modules log through
``logging.getLogger("jarvis.*")``.

Logs go to stderr (uvicorn capture) AND to ``logs/jarvis.log`` so they
survive process restarts and can be tailed independently.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

_CONFIGURED = False


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level_name = (os.environ.get("JARVIS_LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    root = logging.getLogger("jarvis")
    root.setLevel(level)
    root.propagate = False

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)

    try:
        from .config import ROOT as _ROOT

        logs_dir = _ROOT / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(logs_dir / "jarvis.log", encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except Exception:
        pass

    _CONFIGURED = True

