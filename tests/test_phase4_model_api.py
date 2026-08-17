"""Phase 4 API regressions for runner snapshots and local chat preflight."""

from __future__ import annotations

import json

import pytest

from jarvis import app as app_module
from jarvis import local_models
from jarvis.agent import loop as agent_loop


def _snapshot(
    *,
    state: str = "ready",
    loaded_id: str | None = "fake:model",
    target_id: str | None = None,
    error: str | None = None,
    active_streams: int = 0,
) -> dict[str, object]:
    return {
        "engine_available": True,
        "state": state,
        "loaded_id": loaded_id,
        "loaded_tag": loaded_id,
        "target_id": target_id,
        "target_tag": target_id,
        "error": error,
        "active_streams": active_streams,
    }


@pytest.mark.asyncio
async def test_local_models_load_returns_full_runner_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRunner:
        async def load(self, model_id: str) -> dict[str, object]:
            assert model_id == "fake:model"
            return _snapshot()

    monkeypatch.setattr(local_models, "RUNNER", FakeRunner())

    response = await app_module.api_local_models_load(app_module.LocalModelIdIn(model_id="fake:model"))

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["ok"] is True
    assert body["runner"] == _snapshot()


@pytest.mark.asyncio
async def test_local_models_get_returns_runner_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRunner:
        async def astatus(self) -> dict[str, object]:
            return _snapshot(state="loading", loaded_id=None, target_id="fake:model")

        async def discover(self) -> list[dict[str, object]]:
            return []

        def pulls_status(self) -> list[dict[str, object]]:
            return []

    monkeypatch.setattr(local_models, "RUNNER", FakeRunner())

    response = await app_module.api_local_models()

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["runner"] == _snapshot(state="loading", loaded_id=None, target_id="fake:model")
    assert body["models"] == []
    assert body["pulls"] == []


@pytest.mark.asyncio
async def test_local_models_unload_busy_is_structured_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRunner:
        async def unload(self) -> dict[str, object]:
            raise local_models.LocalModelBusyError("lokalni model trenutno generiše odgovor")

        async def astatus(self) -> dict[str, object]:
            return _snapshot(active_streams=1)

    monkeypatch.setattr(local_models, "RUNNER", FakeRunner())

    response = await app_module.api_local_models_unload()

    assert response.status_code == 409
    body = json.loads(response.body)
    assert body["ok"] is False
    assert body["error_code"] == "NOT_AVAILABLE"
    assert body["runner"]["active_streams"] == 1


@pytest.mark.asyncio
async def test_chat_api_rejects_not_ready_local_model_without_cloud_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NotReadyRunner:
        def is_ready(self, model_id: str) -> bool:
            return False

        def status(self) -> dict[str, object]:
            return _snapshot(state="loading", loaded_id=None, target_id="fake:model")

    monkeypatch.setattr(local_models, "RUNNER", NotReadyRunner())
    agent_loop.SESSIONS.clear()

    response = await app_module.api_chat(app_module.ChatIn(text="ne šalji", model="local:fake:model"))

    assert response.status_code == 409
    body = json.loads(response.body)
    assert body == {
        "ok": False,
        "error_code": "NOT_READY",
        "error": "lokalni model 'fake:model' nije spreman za chat (stanje: loading)",
    }
    assert agent_loop.SESSIONS == {}
