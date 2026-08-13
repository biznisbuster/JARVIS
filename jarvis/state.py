"""Shared runtime singletons.

Holds the objects that multiple subsystems need (permission store, lazily
loaded audio models) so modules never import each other through `app.py`.
Everything here is created once at import time and lives for the lifetime
of the server process.
"""

from __future__ import annotations

from .config import SETTINGS
from .permissions import PermissionStore


class _ModelState:
    """Slot for a lazily loaded heavy model (whisper / piper / xtts)."""

    def __init__(self) -> None:
        self.model = None


permission_store = PermissionStore(SETTINGS.permissions_path)

whisper_state = _ModelState()
piper_state = _ModelState()
xtts_state = _ModelState()
