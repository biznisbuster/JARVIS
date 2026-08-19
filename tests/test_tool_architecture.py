"""Direct coverage for the Phase 5 tool contracts and executor boundary."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from jarvis.agent import tools as legacy_tools
from jarvis.tools import (
    DEFAULT_REGISTRY,
    ToolDef,
    ToolErrorCode,
    ToolExecutionContext,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
)
from jarvis.tools import media as media_tools
from jarvis.tools.base import _schema


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, kind: str, payload: dict[str, Any]) -> None:
        self.events.append((kind, payload))

    @property
    def kinds(self) -> list[str]:
        return [kind for kind, _payload in self.events]


class FakePermission:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def check(self, name: str, args: dict[str, Any]) -> bool:
        self.calls.append((name, dict(args)))
        return self.allowed


def definition(
    name: str,
    execute,
    *,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
    timeout_s: float | None = None,
    suppresses_speech: bool = False,
) -> ToolDef:
    return ToolDef(
        name,
        name,
        _schema(name, name, properties or {}, required or []),
        execute,
        timeout_s=timeout_s,
        suppresses_speech=suppresses_speech,
    )


def context(permission: FakePermission | None = None) -> ToolExecutionContext:
    return ToolExecutionContext(session_id="session-test", permission_store=permission or FakePermission())


@pytest.mark.asyncio
async def test_tool_result_serialization_and_legacy_normalization() -> None:
    success = ToolResult.success({"value": 3}, meta={"source": "fake"})
    assert json.loads(success.to_json()) == {"ok": True, "value": 3, "meta": {"source": "fake"}}

    failure = ToolResult.failure(
        ToolErrorCode.VERIFICATION_FAILED,
        "transition was not observed",
        data={"delivered": True, "verified": False, "degraded": True, "verification": "unavailable"},
        details={"attempts": 1},
    )
    failure_payload = json.loads(failure.to_json())
    assert failure_payload == {
        "ok": False,
        "delivered": True,
        "verified": False,
        "degraded": True,
        "verification": "unavailable",
        "error_code": "VERIFICATION_FAILED",
        "error": "transition was not observed",
        "error_details": {"attempts": 1},
    }

    legacy = ToolResult.from_legacy(
        '{"ok": false, "error_code": "VERIFICATION_FAILED", "error": "not verified", '
        '"delivered": true, "verified": false}'
    )
    assert legacy.error is not None
    assert legacy.error.code_value == "VERIFICATION_FAILED"
    assert legacy.to_payload()["delivered"] is True

    malformed = ToolResult.from_legacy("not-json")
    assert malformed.ok is False
    assert malformed.error is not None
    assert malformed.error.code_value == "EXECUTION_FAILED"

    legacy_dict = ToolResult.from_legacy({"ok": False, "error": "plain failure", "items": []})
    assert legacy_dict.error is not None
    assert legacy_dict.error.code_value == "EXECUTION_FAILED"
    assert legacy_dict.data == {"items": []}


@pytest.mark.asyncio
async def test_media_adapter_returns_tool_result_without_losing_verification_fields() -> None:
    class FakeMedia:
        async def pause(self) -> dict[str, Any]:
            return {
                "ok": False,
                "delivered": True,
                "verified": False,
                "degraded": True,
                "verification": "unavailable",
                "error_code": "VERIFICATION_FAILED",
                "error": "pause was not verified",
            }

    result = await media_tools.ytm_pause({}, service=FakeMedia())

    assert isinstance(result, ToolResult)
    assert result.error is not None
    assert result.error.code_value == "VERIFICATION_FAILED"
    assert result.to_payload()["delivered"] is True
    assert result.to_payload()["verification"] == "unavailable"


def test_registry_preserves_public_names_and_schemas() -> None:
    expected = {
        "time_now",
        "reminders_create",
        "reminders_list",
        "calendar_today",
        "open_app",
        "open_url",
        "play_youtube",
        "ytm_play",
        "ytm_pause",
        "ytm_resume",
        "ytm_next",
        "ytm_previous",
        "ytm_volume_up",
        "ytm_volume_down",
        "ytm_volume_set",
        "ytm_volume_mute",
        "ytm_status",
        "web_search",
        "read_clipboard",
        "write_clipboard",
        "system_volume",
        "kilo_run",
    }
    assert {definition.name for definition in DEFAULT_REGISTRY} == expected
    assert len(DEFAULT_REGISTRY.schemas()) == len(DEFAULT_REGISTRY) == len(expected)
    assert all(definition.schema["function"]["name"] == definition.name for definition in DEFAULT_REGISTRY)
    assert legacy_tools.all_schemas() == DEFAULT_REGISTRY.schemas()
    assert legacy_tools.get("ytm_volume_set") is DEFAULT_REGISTRY.get("ytm_volume_set")

    volume_set = DEFAULT_REGISTRY.get("ytm_volume_set")
    assert volume_set is not None
    assert volume_set.schema["function"]["parameters"]["required"] == ["level"]
    assert volume_set.schema["function"]["parameters"]["properties"]["level"]["minimum"] == 0
    assert volume_set.schema["function"]["parameters"]["properties"]["level"]["maximum"] == 100

    for name in ("ytm_volume_up", "ytm_volume_down"):
        definition_ = DEFAULT_REGISTRY.get(name)
        assert definition_ is not None
        amount = definition_.schema["function"]["parameters"]["properties"]["amount"]
        assert amount["default"] == 10
        assert amount["minimum"] == 1
        assert amount["maximum"] == 100

    assert {definition.name for definition in DEFAULT_REGISTRY if definition.suppresses_speech} == {
        "play_youtube",
        "ytm_play",
        "ytm_pause",
        "ytm_resume",
        "ytm_next",
        "ytm_previous",
        "ytm_volume_up",
        "ytm_volume_down",
        "ytm_volume_set",
        "ytm_volume_mute",
    }


async def _noop(args: dict[str, Any]) -> str:
    return json.dumps({"ok": True, "args": args})


def test_registry_rejects_duplicates_and_schema_mismatch() -> None:
    registry = ToolRegistry([definition("one", _noop)])
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(definition("one", _noop))
    with pytest.raises(ValueError, match="mismatch"):
        registry.register(ToolDef("two", "", _schema("not-two", "", {}, []), _noop))
    assert registry.get("missing") is None


@pytest.mark.asyncio
async def test_adding_a_tool_uses_executor_without_loop_changes() -> None:
    executed: list[dict[str, Any]] = []

    async def execute(args: dict[str, Any]) -> str:
        executed.append(args)
        return json.dumps({"ok": True, "value": "registered dynamically"})

    registry = ToolRegistry([definition("new_test_tool", execute)])
    bus = FakeBus()
    executor = ToolExecutor(registry, bus=bus)
    result = await executor.execute_call(
        {"id": "call-new", "function": {"name": "new_test_tool", "arguments": {"x": 1}}},
        context=context(),
    )
    assert executed == [{"x": 1}]
    assert json.loads(result.tool_message()["content"]) == {"ok": True, "value": "registered dynamically"}
    assert bus.kinds == ["tool_call", "tool_done"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ({"id": "bad-json", "function": {"name": "fake", "arguments": '{"broken"'}}, "INVALID_ARGUMENTS"),
        ({"id": "array", "function": {"name": "fake", "arguments": "[]"}}, "INVALID_ARGUMENTS"),
        ({"id": "missing-name", "function": {"name": "", "arguments": "{}"}}, "INVALID_ARGUMENTS"),
        ({"id": "unknown", "function": {"name": "missing", "arguments": "{}"}}, "NOT_AVAILABLE"),
    ],
)
async def test_executor_rejects_malformed_or_unknown_calls_before_permission(
    call: dict[str, Any],
    expected: str,
) -> None:
    executed = False

    async def execute(args: dict[str, Any]) -> str:
        nonlocal executed
        executed = True
        return "{}"

    permission = FakePermission()
    bus = FakeBus()
    executor = ToolExecutor(ToolRegistry([definition("fake", execute)]), bus=bus)
    result = await executor.execute_call(call, context=context(permission))
    assert result.result.error is not None
    assert result.result.error.code_value == expected
    assert executed is False
    assert permission.calls == []
    assert bus.kinds == ["tool_error"]


@pytest.mark.asyncio
async def test_schema_validation_happens_before_permission_and_execution() -> None:
    executed = False

    async def execute(args: dict[str, Any]) -> str:
        nonlocal executed
        executed = True
        return "{}"

    tool = definition(
        "validated",
        execute,
        properties={
            "title": {"type": "string"},
            "level": {"type": "integer", "minimum": 0, "maximum": 100},
        },
        required=["title"],
    )
    permission = FakePermission()
    executor = ToolExecutor(ToolRegistry([tool]), bus=FakeBus())

    for args in ({}, {"title": 123}, {"title": "x", "level": -1}, {"title": "x", "level": 101}):
        result = await executor.execute_call(
            {"function": {"name": "validated", "arguments": json.dumps(args)}},
            context=context(permission),
        )
        assert result.result.error is not None
        assert result.result.error.code_value == "INVALID_ARGUMENTS"

    assert permission.calls == []
    assert executed is False


@pytest.mark.asyncio
async def test_optional_arguments_are_not_invented_by_executor() -> None:
    seen: list[dict[str, Any]] = []

    async def execute(args: dict[str, Any]) -> str:
        seen.append(args)
        return json.dumps({"ok": True})

    tool = definition("optional", execute, properties={"amount": {"type": "integer"}})
    bus = FakeBus()
    result = await ToolExecutor(ToolRegistry([tool]), bus=bus).execute_call(
        {"function": {"name": "optional", "arguments": "{}"}},
        context=context(),
    )
    assert seen == [{}]
    assert result.call_id
    assert result.call_id == result.tool_message()["tool_call_id"]
    assert result.call_id == bus.events[0][1]["call_id"]


@pytest.mark.asyncio
async def test_kilo_timeout_tracks_existing_max_duration_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.tools.coding import kilo as kilo_mod

    captured: list[float] = []

    async def fake_run(prompt: str, *, cwd: str | None = None, timeout: float = 180.0) -> str:
        captured.append(timeout)
        return json.dumps({"ok": True})

    monkeypatch.setattr(kilo_mod, "run_kilo", fake_run)
    result = await kilo_mod.kilo_run_tool({"prompt": "safe check", "max_duration_s": 42})
    assert json.loads(result)["ok"] is True
    assert captured == [42]


@pytest.mark.asyncio
async def test_permission_denial_is_canonical_and_does_not_suppress_or_execute() -> None:
    executed = False
    suppressed: list[str] = []

    async def execute(args: dict[str, Any]) -> str:
        nonlocal executed
        executed = True
        return "{}"

    bus = FakeBus()
    executor = ToolExecutor(
        ToolRegistry([definition("audio", execute, suppresses_speech=True)]),
        bus=bus,
        speech_suppressor=suppressed.append,
    )
    result = await executor.execute_call(
        {"id": "denied-call", "function": {"name": "audio", "arguments": "{}"}},
        context=context(FakePermission(allowed=False)),
    )
    assert result.result.error is not None
    assert result.result.error.code_value == "PERMISSION_DENIED"
    assert result.result.to_payload()["denied"] is True
    assert executed is False
    assert suppressed == []
    assert bus.kinds == ["tool_call", "tool_done"]
    assert bus.events[-1][1]["denied"] is True


@pytest.mark.asyncio
async def test_speech_suppression_is_metadata_and_happens_after_permission() -> None:
    calls: list[str] = []

    async def execute(args: dict[str, Any]) -> str:
        assert calls == ["session-test"]
        return json.dumps({"ok": True})

    bus = FakeBus()
    executor = ToolExecutor(
        ToolRegistry([definition("audio", execute, suppresses_speech=True), definition("normal", execute)]),
        bus=bus,
        speech_suppressor=calls.append,
    )
    await executor.execute_call(
        {"function": {"name": "audio", "arguments": "{}"}},
        context=context(),
    )
    await executor.execute_call(
        {"function": {"name": "normal", "arguments": "{}"}},
        context=context(),
    )
    assert calls == ["session-test"]


@pytest.mark.asyncio
async def test_speech_suppression_failure_is_a_single_executor_error() -> None:
    executed = False

    async def execute(args: dict[str, Any]) -> str:
        nonlocal executed
        executed = True
        return json.dumps({"ok": True})

    async def suppress(_session_id: str) -> None:
        raise RuntimeError("speech service unavailable")

    bus = FakeBus()
    executor = ToolExecutor(
        ToolRegistry([definition("audio", execute, suppresses_speech=True)]),
        bus=bus,
        speech_suppressor=suppress,
    )
    result = await executor.execute_call(
        {"id": "speech-failure", "function": {"name": "audio", "arguments": "{}"}},
        context=context(),
    )

    assert result.terminal_event == "tool_error"
    assert result.result.error is not None
    assert result.result.error.code_value == "EXECUTION_FAILED"
    assert executed is False
    assert bus.kinds == ["tool_call", "tool_error"]


@pytest.mark.asyncio
async def test_permission_wait_is_outside_execution_timeout_and_override_is_honored() -> None:
    release = asyncio.Event()
    permission_started = asyncio.Event()
    executed: list[str] = []

    class DelayedPermission(FakePermission):
        async def check(self, name: str, args: dict[str, Any]) -> bool:
            self.calls.append((name, dict(args)))
            permission_started.set()
            await release.wait()
            return True

    async def slow_enough(args: dict[str, Any]) -> str:
        executed.append("slow_enough")
        await asyncio.sleep(0.02)
        return json.dumps({"ok": True})

    tool = definition("slow_enough", slow_enough, timeout_s=0.05)
    permission = DelayedPermission()
    bus = FakeBus()
    task = asyncio.create_task(
        ToolExecutor(ToolRegistry([tool]), default_timeout_s=0.005, bus=bus).execute_call(
            {"function": {"name": "slow_enough", "arguments": "{}"}},
            context=context(permission),
        )
    )
    await asyncio.wait_for(permission_started.wait(), timeout=1)
    await asyncio.sleep(0.02)
    assert not task.done(), "human approval time must not consume execution timeout"
    release.set()
    result = await task
    assert result.result.ok is True
    assert executed == ["slow_enough"]


@pytest.mark.asyncio
async def test_default_and_per_tool_timeout_produce_one_terminal_event_and_recover() -> None:
    async def slow(args: dict[str, Any]) -> str:
        await asyncio.sleep(0.05)
        return json.dumps({"ok": True})

    async def fast(args: dict[str, Any]) -> str:
        return json.dumps({"ok": True, "fast": True})

    bus = FakeBus()
    executor = ToolExecutor(
        ToolRegistry(
            [
                definition("slow", slow),
                definition("slow_override", slow, timeout_s=0.1),
                definition("fast", fast),
            ]
        ),
        default_timeout_s=0.01,
        bus=bus,
    )
    timed_out = await executor.execute_call(
        {"id": "slow-call", "function": {"name": "slow", "arguments": "{}"}},
        context=context(),
    )
    assert timed_out.result.error is not None
    assert timed_out.result.error.code_value == "TIMEOUT"
    assert [kind for kind, payload in bus.events if payload.get("call_id") == "slow-call"] == [
        "tool_call",
        "tool_error",
    ]

    override = await executor.execute_call(
        {"id": "override-call", "function": {"name": "slow_override", "arguments": "{}"}},
        context=context(),
    )
    assert override.result.ok is True

    recovered = await executor.execute_call(
        {"id": "fast-call", "function": {"name": "fast", "arguments": "{}"}},
        context=context(),
    )
    assert recovered.result.ok is True


@pytest.mark.asyncio
async def test_cancellation_propagates_without_fake_result_or_terminal_event() -> None:
    started = asyncio.Event()

    async def never_finishes(args: dict[str, Any]) -> str:
        started.set()
        await asyncio.Event().wait()
        return "{}"

    bus = FakeBus()
    executor = ToolExecutor(
        ToolRegistry([definition("cancel_me", never_finishes, timeout_s=10)]),
        bus=bus,
    )
    task = asyncio.create_task(
        executor.execute_call(
            {"id": "cancel-call", "function": {"name": "cancel_me", "arguments": "{}"}},
            context=context(),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [kind for kind, _payload in bus.events] == ["tool_call"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (FileNotFoundError("osascript"), "DEPENDENCY_MISSING"),
        (ModuleNotFoundError("optional_package"), "DEPENDENCY_MISSING"),
        (RuntimeError("boom"), "EXECUTION_FAILED"),
    ],
)
async def test_executor_normalizes_implementation_exceptions(
    exception: Exception,
    expected: str,
) -> None:
    async def execute(args: dict[str, Any]) -> str:
        raise exception

    bus = FakeBus()
    result = await ToolExecutor(
        ToolRegistry([definition("raises", execute)]),
        bus=bus,
    ).execute_call(
        {"function": {"name": "raises", "arguments": "{}"}},
        context=context(),
    )
    assert result.result.error is not None
    assert result.result.error.code_value == expected
    assert bus.kinds == ["tool_call", "tool_error"]
