"""Phase 0 regression coverage for the local Ollama adapter boundary."""

from __future__ import annotations

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
