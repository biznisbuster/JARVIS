"""Phase 0 characterization tests for the known desktop YTM transport bug."""

from __future__ import annotations

import pytest

from jarvis.agent import tools


async def _true() -> bool:
    return True


async def _pid() -> int:
    return 123


@pytest.mark.xfail(
    strict=True,
    reason="known Phase 1 defect: desktop next sends an unconditional play/pause toggle",
)
async def test_next_does_not_send_unconditional_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[int] = []

    async def fake_post(pid: int, key_code: int) -> bool:
        sent.append(key_code)
        return True

    monkeypatch.setattr(tools._ytm_web, "is_available", lambda: False)
    monkeypatch.setattr(tools, "_ytm_app_installed", lambda: True)
    monkeypatch.setattr(tools, "_ytm_is_running", _true)
    monkeypatch.setattr(tools, "_ytm_pid", _pid)
    monkeypatch.setattr(tools, "_ytm_post_keycode", fake_post)

    result = await tools._ytm_send_transport("next")

    assert result["ok"] is True
    assert sent == [tools._YTM_KEY_CODES["next"]]


@pytest.mark.xfail(
    strict=True,
    reason="known Phase 1 defect: desktop previous sends an unconditional play/pause toggle",
)
async def test_previous_does_not_send_unconditional_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[int] = []

    async def fake_post(pid: int, key_code: int) -> bool:
        sent.append(key_code)
        return True

    monkeypatch.setattr(tools._ytm_web, "is_available", lambda: False)
    monkeypatch.setattr(tools, "_ytm_app_installed", lambda: True)
    monkeypatch.setattr(tools, "_ytm_is_running", _true)
    monkeypatch.setattr(tools, "_ytm_pid", _pid)
    monkeypatch.setattr(tools, "_ytm_post_keycode", fake_post)

    result = await tools._ytm_send_transport("previous")

    assert result["ok"] is True
    assert sent == [tools._YTM_KEY_CODES["previous"]]
