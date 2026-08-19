"""Compatibility exports for the Kilo tool bridge.

The implementation now lives in :mod:`jarvis.tools.coding.kilo`; this module
remains only for callers that imported the historical agent path.
"""

from __future__ import annotations

from ..tools.coding.kilo import _clean, _which, kilo_run_tool, run_kilo

__all__ = ["_clean", "_which", "kilo_run_tool", "run_kilo"]
