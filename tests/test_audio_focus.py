"""Regression tests for listen-mode audio focus restoration."""

from __future__ import annotations

import pytest

from jarvis.audio import focus as focus_mod


@pytest.mark.asyncio
async def test_wait_until_released_waits_for_debounced_volume_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(focus_mod, "_RESTORE_COOLDOWN_S", 0.01)

    async def read_volume() -> int:
        return 42

    monkeypatch.setattr(focus_mod, "_read_output_volume", read_volume)
    restored: list[int] = []

    async def set_volume(value: int) -> None:
        restored.append(value)

    monkeypatch.setattr(focus_mod, "_set_output_volume", set_volume)

    manager = focus_mod.AudioFocusManager()
    await manager.enter("ptt")
    await manager.exit("ptt")
    await manager.wait_until_released()

    assert restored == [0, 42]
