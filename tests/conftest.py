"""Shared fixtures for the Jarvis test suite."""

from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def free_port() -> int:
    return _free_port()


@pytest.fixture
def tmp_data_dir(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test data directory; isolates state.json / sessions.json."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d)
        try:
            from jarvis import state_store
            from jarvis.agent import loop as loop_mod

            monkeypatch.setattr(state_store, "_STATE_FILE", path / "state.json", raising=False)
            monkeypatch.setattr(loop_mod, "SESSIONS_FILE", path / "sessions.json", raising=False)
        except Exception:
            pass
        yield path


@pytest.fixture
def env_clean(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    for k in list(os.environ):
        if k.startswith("JARVIS_") or k == "KILO_CONFIG":
            monkeypatch.delenv(k, raising=False)
    return {}


@pytest.fixture
def permission_store(tmp_path: Path):
    from jarvis.permissions import PermissionStore

    yield PermissionStore(tmp_path / "permissions.json")
