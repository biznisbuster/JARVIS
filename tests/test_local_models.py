"""Phase 0 regression coverage for the local Ollama adapter boundary."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Iterable
from typing import Any

import pytest

from jarvis import local_models
from jarvis.config import SETTINGS, LocalModelSettings
from tests.fakes.ollama import (
    FakeAsyncOllamaClient,
    FakeStreamResponse,
    FakeSyncOllamaClient,
    FakeSyncResponse,
)


def _sse(events: Iterable[dict[str, Any]]) -> list[str]:
    return [f"data: {json.dumps(event)}" for event in events]


def _choice_tool(*tool_calls: dict[str, Any]) -> dict[str, Any]:
    return {"choices": [{"delta": {"tool_calls": list(tool_calls)}}]}


def _ready_runner() -> local_models.LocalModelRunner:
    runner = local_models.LocalModelRunner()
    runner._state = "ready"
    runner._loaded_id = "fake:model"
    runner._loaded_tag = "fake:model"
    return runner


def _patch_lifecycle_fakes(
    monkeypatch: pytest.MonkeyPatch,
    runner: local_models.LocalModelRunner,
    *,
    ps_tags: list[str] | None = None,
    warmup=None,
) -> None:
    async def available() -> bool:
        return True

    async def has_model(_tag: str) -> bool:
        return True

    async def fake_warmup(tag: str, *, keep_alive: str) -> None:
        if warmup is not None:
            await warmup(tag, keep_alive=keep_alive)

    async def fake_ps_tags() -> list[str]:
        return list(ps_tags or [])

    async def fake_probe(_tag: str) -> str:
        return "tools"

    monkeypatch.setattr(runner, "available_async", available)
    monkeypatch.setattr(runner, "_has_model", has_model)
    monkeypatch.setattr(runner, "_warmup", fake_warmup)
    monkeypatch.setattr(runner, "_ps_tags", fake_ps_tags)
    monkeypatch.setattr(runner, "probe_tools", fake_probe)


@pytest.mark.asyncio
async def test_loading_snapshot_keeps_loaded_id_confirmed_only(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = local_models.LocalModelRunner()
    started = asyncio.Event()
    release = asyncio.Event()

    async def warmup(_tag: str, *, keep_alive: str) -> None:
        started.set()
        await release.wait()

    _patch_lifecycle_fakes(monkeypatch, runner, ps_tags=["fake:model"], warmup=warmup)

    task = asyncio.create_task(runner.load("fake:model"))
    await started.wait()

    loading = await runner.astatus()
    assert loading["state"] == "loading"
    assert loading["loaded_id"] is None
    assert loading["loaded_tag"] is None
    assert loading["target_id"] == "fake:model"
    assert loading["target_tag"] == "fake:model"
    assert not runner.is_ready("fake:model")

    release.set()
    ready = await task
    assert ready["state"] == "ready"
    assert ready["loaded_id"] == "fake:model"
    assert ready["loaded_tag"] == "fake:model"
    assert ready["target_id"] is None


@pytest.mark.asyncio
async def test_same_ready_model_load_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _ready_runner()
    warmups = 0

    async def warmup(_tag: str, *, keep_alive: str) -> None:
        nonlocal warmups
        warmups += 1

    _patch_lifecycle_fakes(monkeypatch, runner, ps_tags=["fake:model"], warmup=warmup)

    result = await runner.load("fake:model")

    assert result["state"] == "ready"
    assert result["loaded_id"] == "fake:model"
    assert warmups == 0


@pytest.mark.asyncio
async def test_load_failure_restores_previous_ready_model_when_still_resident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = local_models.LocalModelRunner()
    runner._state = "ready"
    runner._loaded_id = "old:model"
    runner._loaded_tag = "old:model"

    async def available() -> bool:
        return True

    async def has_model(_tag: str) -> bool:
        return True

    async def warmup(_tag: str, *, keep_alive: str) -> None:
        raise RuntimeError("warmup failed")

    async def ps_tags() -> list[str]:
        return ["old:model"]

    monkeypatch.setattr(runner, "available_async", available)
    monkeypatch.setattr(runner, "_has_model", has_model)
    monkeypatch.setattr(runner, "_warmup", warmup)
    monkeypatch.setattr(runner, "_ps_tags", ps_tags)

    with pytest.raises(RuntimeError, match="warmup failed"):
        await runner.load("new:model")

    status = await runner.astatus()
    assert status["state"] == "ready"
    assert status["loaded_id"] == "old:model"
    assert status["target_id"] is None
    assert status["error"] == "warmup failed"
    assert runner.is_ready("old:model")
    assert not runner.is_ready("new:model")


@pytest.mark.asyncio
async def test_load_failure_without_previous_model_enters_error_and_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = local_models.LocalModelRunner()

    async def available() -> bool:
        return True

    async def has_model(_tag: str) -> bool:
        return True

    async def warmup(_tag: str, *, keep_alive: str) -> None:
        raise RuntimeError("missing resident model")

    async def ps_tags() -> list[str]:
        return []

    monkeypatch.setattr(runner, "available_async", available)
    monkeypatch.setattr(runner, "_has_model", has_model)
    monkeypatch.setattr(runner, "_warmup", warmup)
    monkeypatch.setattr(runner, "_ps_tags", ps_tags)

    with pytest.raises(RuntimeError, match="missing resident model"):
        await runner.load("fake:model")

    status = await runner.astatus()
    assert status["state"] == "error"
    assert status["loaded_id"] is None
    assert status["target_id"] is None
    assert not runner.is_ready("fake:model")


@pytest.mark.asyncio
async def test_concurrent_loads_are_serialized_and_last_completed_request_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = local_models.LocalModelRunner()
    warmups: list[str] = []

    async def warmup(tag: str, *, keep_alive: str) -> None:
        warmups.append(tag)
        await asyncio.sleep(0)

    _patch_lifecycle_fakes(monkeypatch, runner, ps_tags=["a:model", "b:model"], warmup=warmup)

    first, second = await asyncio.gather(
        runner.load("a:model"),
        runner.load("b:model"),
    )

    assert [first["loaded_id"], second["loaded_id"]] == ["a:model", "b:model"]
    assert warmups == ["a:model", "b:model"]
    assert (await runner.astatus())["loaded_id"] == "b:model"


@pytest.mark.asyncio
async def test_same_model_concurrent_load_does_not_duplicate_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = local_models.LocalModelRunner()
    started = asyncio.Event()
    release = asyncio.Event()
    warmups = 0

    async def warmup(_tag: str, *, keep_alive: str) -> None:
        nonlocal warmups
        warmups += 1
        started.set()
        await release.wait()

    _patch_lifecycle_fakes(monkeypatch, runner, ps_tags=["fake:model"], warmup=warmup)

    first = asyncio.create_task(runner.load("fake:model"))
    await started.wait()
    second = asyncio.create_task(runner.load("fake:model"))
    await asyncio.sleep(0)
    assert not second.done()

    release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result["loaded_id"] == second_result["loaded_id"] == "fake:model"
    assert warmups == 1


@pytest.mark.asyncio
async def test_unload_reports_unloading_and_then_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _ready_runner()
    started = asyncio.Event()
    release = asyncio.Event()

    async def available() -> bool:
        return True

    async def unload_tag(_tag: str) -> None:
        started.set()
        await release.wait()

    async def ps_tags() -> list[str]:
        return []

    monkeypatch.setattr(runner, "available_async", available)
    monkeypatch.setattr(runner, "_unload_tag", unload_tag)
    monkeypatch.setattr(runner, "_ps_tags", ps_tags)

    task = asyncio.create_task(runner.unload())
    await started.wait()

    unloading = await runner.astatus()
    assert unloading["state"] == "unloading"
    assert unloading["loaded_id"] == "fake:model"
    assert unloading["target_id"] == "fake:model"
    assert not runner.is_ready("fake:model")

    release.set()
    idle = await task
    assert idle["state"] == "idle"
    assert idle["loaded_id"] is None
    assert idle["target_id"] is None


@pytest.mark.asyncio
async def test_unload_waits_for_delayed_residency_eviction(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _ready_runner()
    checks = 0

    async def available() -> bool:
        return True

    async def unload_tag(_tag: str) -> None:
        return None

    async def ps_tags() -> list[str]:
        nonlocal checks
        checks += 1
        return ["fake:model"] if checks < 3 else []

    monkeypatch.setattr(runner, "available_async", available)
    monkeypatch.setattr(runner, "_unload_tag", unload_tag)
    monkeypatch.setattr(runner, "_ps_tags", ps_tags)

    result = await runner.unload()

    assert result["state"] == "idle"
    assert checks == 3


@pytest.mark.asyncio
async def test_unload_failure_does_not_claim_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _ready_runner()

    async def available() -> bool:
        return True

    async def unload_tag(_tag: str) -> None:
        raise RuntimeError("unload failed")

    async def ps_tags() -> list[str]:
        return ["fake:model"]

    monkeypatch.setattr(runner, "available_async", available)
    monkeypatch.setattr(runner, "_unload_tag", unload_tag)
    monkeypatch.setattr(runner, "_ps_tags", ps_tags)

    with pytest.raises(RuntimeError, match="unload failed"):
        await runner.unload()

    status = await runner.astatus()
    assert status["state"] == "ready"
    assert status["loaded_id"] == "fake:model"
    assert status["error"] == "unload failed"


@pytest.mark.asyncio
async def test_unload_rejects_active_local_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _ready_runner()
    runner._active_streams = 1
    unload_called = False

    async def unload_tag(_tag: str) -> None:
        nonlocal unload_called
        unload_called = True

    monkeypatch.setattr(runner, "_unload_tag", unload_tag)

    with pytest.raises(local_models.LocalModelBusyError):
        await runner.unload()

    assert unload_called is False
    assert (await runner.astatus())["state"] == "ready"


@pytest.mark.asyncio
async def test_active_stream_count_returns_to_zero_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _ready_runner()
    client = FakeAsyncOllamaClient(
        _sse([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}])
    )
    monkeypatch.setattr(local_models.httpx, "AsyncClient", lambda **kwargs: client)

    events = [event async for event in runner.stream_chat("fake:model", [{"role": "user", "content": "hi"}])]

    assert next(value for kind, value in events if kind == "done")["content"] == "ok"
    assert runner._active_streams == 0


@pytest.mark.asyncio
async def test_active_stream_count_returns_to_zero_after_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _ready_runner()

    class FailingResponse:
        status_code = 500

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def aread(self) -> bytes:
            return b"boom"

    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def stream(self, method: str, url: str, **kwargs: Any) -> FailingResponse:
            return FailingResponse()

    client = FailingClient()
    monkeypatch.setattr(local_models.httpx, "AsyncClient", lambda **kwargs: client)

    with pytest.raises(RuntimeError, match="Ollama HTTP 500"):
        _ = [event async for event in runner.stream_chat("fake:model", [{"role": "user", "content": "hi"}])]

    assert runner._active_streams == 0


@pytest.mark.asyncio
async def test_active_stream_count_returns_to_zero_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _ready_runner()
    entered = asyncio.Event()

    class BlockingResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def aiter_lines(self):
            entered.set()
            await asyncio.Event().wait()
            yield ""

    class BlockingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def stream(self, method: str, url: str, **kwargs: Any) -> BlockingResponse:
            return BlockingResponse()

    monkeypatch.setattr(local_models.httpx, "AsyncClient", lambda **kwargs: BlockingClient())

    async def consume() -> None:
        async for _event in runner.stream_chat("fake:model", [{"role": "user", "content": "hi"}]):
            pass

    task = asyncio.create_task(consume())
    await entered.wait()
    assert runner._active_streams == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner._active_streams == 0


async def _run_runtime_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
    *tool_calls: dict[str, Any],
) -> tuple[local_models.LocalModelRunner, list[tuple[str, Any]], dict[str, Any]]:
    runner = _ready_runner()
    persisted: dict[str, Any] = {}

    async def fake_set_state(key: str, value: object) -> None:
        persisted[key] = value

    monkeypatch.setattr(local_models.state_store, "get_state_value", _empty_state)
    monkeypatch.setattr(local_models.state_store, "set_state_value", fake_set_state)
    client = FakeAsyncOllamaClient(
        _sse(
            [
                _choice_tool(*tool_calls),
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ]
        )
    )
    monkeypatch.setattr(local_models.httpx, "AsyncClient", lambda **kwargs: client)

    events = [
        event
        async for event in runner.stream_chat(
            "fake:model",
            [{"role": "user", "content": "call a tool"}],
            tools=[{"type": "function", "function": {"name": "time_now"}}],
        )
    ]
    return runner, events, persisted


async def test_local_fragmented_tool_call_is_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _ready_runner()
    lines = _sse(
        [
            _choice_tool(
                {
                    "index": 0,
                    "id": "call-0",
                    "function": {"name": "rem", "arguments": '{"query":'},
                }
            ),
            _choice_tool({"index": 0, "function": {"name": "inders_create", "arguments": ' "x"}'}}),
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    client = FakeAsyncOllamaClient(lines)
    monkeypatch.setattr(local_models.httpx, "AsyncClient", lambda **kwargs: client)

    events = [
        event
        async for event in runner.stream_chat(
            "fake:model",
            [{"role": "user", "content": "set a reminder"}],
            tools=[{"type": "function", "function": {"name": "reminders_create"}}],
        )
    ]

    done = next(value for kind, value in events if kind == "done")
    assert done["tool_calls"] == [
        {
            "id": "call-0",
            "type": "function",
            "function": {
                "name": "reminders_create",
                "arguments": '{"query": "x"}',
            },
        }
    ]


async def test_local_multiple_tool_calls_are_assembled_by_index(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _ready_runner()
    lines = _sse(
        [
            _choice_tool(
                {
                    "index": 1,
                    "id": "call-1",
                    "function": {"name": "time", "arguments": ""},
                }
            ),
            _choice_tool(
                {
                    "index": 0,
                    "id": "call-0",
                    "function": {"name": "rem", "arguments": '{"title":'},
                }
            ),
            _choice_tool({"index": 1, "function": {"name": "_now", "arguments": "{}"}}),
            _choice_tool({"index": 0, "function": {"name": "inders_create", "arguments": ' "x"}'}}),
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    client = FakeAsyncOllamaClient(lines)
    monkeypatch.setattr(local_models.httpx, "AsyncClient", lambda **kwargs: client)

    events = [
        event
        async for event in runner.stream_chat(
            "fake:model",
            [{"role": "user", "content": "do both"}],
            tools=[{"type": "function", "function": {"name": "reminders_create"}}],
        )
    ]

    done = next(value for kind, value in events if kind == "done")
    assert done["tool_calls"] == [
        {
            "id": "call-0",
            "type": "function",
            "function": {
                "name": "reminders_create",
                "arguments": '{"title": "x"}',
            },
        },
        {
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "time_now",
                "arguments": "{}",
            },
        },
    ]


async def test_tool_capability_probe_requires_real_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = local_models.LocalModelRunner()
    response = FakeSyncResponse({"message": {"content": "4"}})
    client = FakeSyncOllamaClient(response)
    monkeypatch.setattr(local_models.httpx, "Client", lambda **kwargs: client)

    async def fake_get_state(key: str, default: Any) -> dict[str, str]:
        return {}

    async def fake_set_state(key: str, value: object) -> None:
        return None

    monkeypatch.setattr(local_models.state_store, "get_state_value", fake_get_state)
    monkeypatch.setattr(local_models.state_store, "set_state_value", fake_set_state)

    result = await runner.probe_tools("fake:model")

    assert result == "notools"


@pytest.mark.asyncio
async def test_tool_capability_probe_accepts_only_matching_structured_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = local_models.LocalModelRunner()
    response = FakeSyncResponse(
        {
            "message": {
                "role": "assistant",
                "tool_calls": [{"function": {"name": "time_now", "arguments": {}}}],
            }
        }
    )
    client = FakeSyncOllamaClient(response)
    monkeypatch.setattr(local_models.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(local_models.state_store, "get_state_value", _empty_state)
    monkeypatch.setattr(local_models.state_store, "set_state_value", _ignore_state)

    assert await runner.probe_tools("fake:model") == "tools"


@pytest.mark.asyncio
async def test_explicit_unsupported_tools_response_is_notools(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = local_models.LocalModelRunner()
    response = FakeSyncResponse({}, status_code=400, text="model does not support tools")
    client = FakeSyncOllamaClient(response)
    monkeypatch.setattr(local_models.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(local_models.state_store, "get_state_value", _empty_state)
    monkeypatch.setattr(local_models.state_store, "set_state_value", _ignore_state)

    assert await runner.probe_tools("fake:model") == "notools"


@pytest.mark.asyncio
async def test_network_probe_failure_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = local_models.LocalModelRunner()

    class FailingClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> FailingClient:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def post(self, url: str, **kwargs: Any) -> FakeSyncResponse:
            raise OSError("offline")

    monkeypatch.setattr(local_models.httpx, "Client", FailingClient)
    monkeypatch.setattr(local_models.state_store, "get_state_value", _empty_state)
    monkeypatch.setattr(local_models.state_store, "set_state_value", _ignore_state)

    assert await runner.probe_tools("fake:model") == "unknown"


async def _empty_state(key: str, default: Any = None) -> Any:
    return default


async def _ignore_state(key: str, value: object) -> None:
    return None


def _settings_with_entries(*flags: str) -> Any:
    entries = [
        {
            "id": "fake-model",
            "label": "Fake model",
            "tag": "fake:model",
            "n_ctx": 32768,
            "keep_alive": "24h",
            "flags": ",".join(flags),
        }
    ]
    return dataclasses.replace(SETTINGS, local_models=LocalModelSettings(entries=entries))


@pytest.mark.asyncio
async def test_explicit_tools_override_cached_notools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_models, "SETTINGS", _settings_with_entries("tools"))
    runner = local_models.LocalModelRunner()
    runner._capabilities = {"fake:model": {"capability": "notools", "identity": {}}}

    assert runner.capability_for("fake:model") == "tools"
    assert await runner.probe_tools("fake:model") == "tools"


@pytest.mark.asyncio
async def test_explicit_notools_override_cached_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_models, "SETTINGS", _settings_with_entries("notools"))
    runner = local_models.LocalModelRunner()
    runner._capabilities = {"fake:model": {"capability": "tools", "identity": {}}}

    assert runner.capability_for("fake:model") == "notools"
    assert await runner.probe_tools("fake:model") == "notools"


@pytest.mark.asyncio
async def test_same_model_identity_reuses_cached_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_state(key: str, default: Any = None) -> Any:
        return {
            "fake:model": {
                "capability": "tools",
                "identity": {"digest": "sha256:one", "modified_at": "now", "size": 10},
            }
        }

    async def fake_tags() -> list[dict[str, Any]]:
        records = [{"name": "fake:model", "digest": "sha256:one", "modified_at": "now", "size": 10}]
        runner._remember_tag_identities(records)
        return records

    runner = local_models.LocalModelRunner()
    monkeypatch.setattr(local_models.state_store, "get_state_value", fake_get_state)
    monkeypatch.setattr(runner, "_api_tags", fake_tags)

    assert await runner.capability_for_model("fake:model") == "tools"


@pytest.mark.asyncio
async def test_changed_model_identity_invalidates_cached_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_state(key: str, default: Any = None) -> Any:
        return {
            "fake:model": {
                "capability": "tools",
                "identity": {"digest": "sha256:old", "modified_at": "old", "size": 10},
            }
        }

    async def fake_tags() -> list[dict[str, Any]]:
        records = [{"name": "fake:model", "digest": "sha256:new", "modified_at": "new", "size": 11}]
        runner._remember_tag_identities(records)
        return records

    runner = local_models.LocalModelRunner()
    monkeypatch.setattr(local_models.state_store, "get_state_value", fake_get_state)
    monkeypatch.setattr(runner, "_api_tags", fake_tags)

    assert await runner.capability_for_model("fake:model") == "unknown"


def test_legacy_capability_cache_normalizes_without_crashing() -> None:
    normalized = local_models.LocalModelRunner._normalize_capability_cache({"fake:model": "tools"})

    assert normalized == {"fake:model": {"capability": "tools", "identity": None}}


def test_notools_history_does_not_leak_tool_mechanics_or_mutate_source() -> None:
    messages = [
        {"role": "user", "content": "Koliko je sati?"},
        {
            "role": "assistant",
            "content": "Pozvaću time_now.",
            "tool_calls": [{"id": "call-1", "function": {"name": "time_now", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "time_now", "content": "12:00"},
        {"role": "assistant", "content": "Sada je podne."},
        {"role": "user", "content": "Hvala."},
    ]

    derived = local_models.sanitize_history_for_notools(messages)

    assert derived == [
        {"role": "user", "content": "Koliko je sati?"},
        {"role": "assistant", "content": "Sada je podne."},
        {"role": "user", "content": "Hvala."},
    ]
    assert messages[1].get("tool_calls")
    assert messages[2]["role"] == "tool"


@pytest.mark.asyncio
async def test_runtime_tool_rejection_retries_once_with_sanitized_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _ready_runner()
    runner._capabilities = {}
    first = FakeStreamResponse([], status_code=400, body=b"model does not support tools")
    second = FakeStreamResponse(
        _sse([{"choices": [{"delta": {"content": "Ne mogu da izvršim akciju."}, "finish_reason": "stop"}]}])
    )

    class SequenceClient:
        def __init__(self, **kwargs: Any) -> None:
            self.responses = [first, second]
            self.requests: list[dict[str, Any]] = []

        async def __aenter__(self) -> SequenceClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def stream(self, method: str, url: str, **kwargs: Any) -> FakeStreamResponse:
            self.requests.append({"method": method, "url": url, "kwargs": kwargs})
            return self.responses.pop(0)

    client = SequenceClient()
    monkeypatch.setattr(local_models.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(local_models.state_store, "set_state_value", _ignore_state)

    history = [
        {"role": "user", "content": "Koliko je sati?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "time_now", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "time_now", "content": "12:00"},
    ]
    events = [
        event
        async for event in runner.stream_chat(
            "fake:model",
            history,
            tools=[{"type": "function", "function": {"name": "time_now"}}],
        )
    ]

    assert len(client.requests) == 2
    first_body = client.requests[0]["kwargs"]["json"]
    second_body = client.requests[1]["kwargs"]["json"]
    assert first_body["tools"]
    assert "tools" not in second_body
    assert all(message["role"] != "tool" for message in second_body["messages"])
    assert all("tool_calls" not in message for message in second_body["messages"])
    assert next(value for kind, value in events if kind == "done")["tool_calls"] == []


@pytest.mark.asyncio
async def test_malformed_runtime_call_is_preserved_but_not_capability_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, events, persisted = await _run_runtime_tool_calls(
        monkeypatch,
        {
            "index": 0,
            "id": "call-bad",
            "function": {"name": "time_now", "arguments": '{"broken"'},
        },
    )

    done = next(value for kind, value in events if kind == "done")
    assert done["tool_calls"][0]["function"]["arguments"] == '{"broken"'
    assert runner.capability_for("fake:model") == "unknown"
    assert "local_model_capabilities" not in persisted


@pytest.mark.asyncio
async def test_non_object_runtime_arguments_are_not_capability_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, events, persisted = await _run_runtime_tool_calls(
        monkeypatch,
        {
            "index": 0,
            "id": "call-list",
            "function": {"name": "time_now", "arguments": "[]"},
        },
    )

    done = next(value for kind, value in events if kind == "done")
    assert done["tool_calls"][0]["function"]["arguments"] == "[]"
    assert runner.capability_for("fake:model") == "unknown"
    assert "local_model_capabilities" not in persisted


@pytest.mark.asyncio
async def test_valid_runtime_call_persists_tools_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, events, persisted = await _run_runtime_tool_calls(
        monkeypatch,
        {
            "index": 0,
            "id": "call-valid",
            "function": {"name": "time_now", "arguments": "{}"},
        },
    )

    done = next(value for kind, value in events if kind == "done")
    assert done["tool_calls"][0]["function"] == {"name": "time_now", "arguments": "{}"}
    assert runner.capability_for("fake:model") == "tools"
    assert persisted["local_model_capabilities"]["fake:model"]["capability"] == "tools"


@pytest.mark.asyncio
async def test_valid_runtime_call_outweighs_malformed_call_for_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, events, persisted = await _run_runtime_tool_calls(
        monkeypatch,
        {
            "index": 0,
            "id": "call-bad",
            "function": {"name": "time_now", "arguments": '{"broken"'},
        },
        {
            "index": 1,
            "id": "call-valid",
            "function": {"name": "time_now", "arguments": "{}"},
        },
    )

    done = next(value for kind, value in events if kind == "done")
    assert len(done["tool_calls"]) == 2
    assert done["tool_calls"][0]["function"]["arguments"] == '{"broken"'
    assert done["tool_calls"][1]["function"]["arguments"] == "{}"
    assert runner.capability_for("fake:model") == "tools"
    assert persisted["local_model_capabilities"]["fake:model"]["capability"] == "tools"
