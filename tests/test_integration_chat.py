"""Integration tests: full agent turn against a fake streaming LLM server.

No external services are touched: world-state, TTS synthesis and audio
playback are stubbed; the LLM is a local FastAPI app streaming OpenAI-style
SSE. Covers: plain chat turn, tool-call loop, FIFO of rapid messages and
barge-in history repair.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from jarvis import llm as llm_mod
from jarvis import local_models, state_store
from jarvis.agent import loop as agent_loop
from jarvis.audio import player as player_mod
from jarvis.audio import speech as speech_mod
from jarvis.audio import tts as tts_mod
from jarvis.bus import BUS
from jarvis.config import SETTINGS
from jarvis.permissions import PermissionStore


def _sse(events: list[dict[str, Any]]) -> str:
    out = []
    for evt in events:
        out.append(f"data: {json.dumps(evt)}\n\n")
    out.append("data: [DONE]\n\n")
    return "".join(out)


def _content_events(text: str, chunk: int = 6) -> list[dict[str, Any]]:
    events = [{"choices": [{"delta": {"content": text[i : i + chunk]}}]} for i in range(0, len(text), chunk)]
    events.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    return events


def _tool_events() -> list[dict[str, Any]]:
    return [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_t1",
                                "function": {"name": "time_now", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]


def _build_fake_app() -> FastAPI:
    app = FastAPI()

    @app.post("/chat/completions")
    async def chat_completions(request: Request):
        body = await request.json()
        messages = body.get("messages") or []
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = str(m.get("content") or "")
                break
        if any(m.get("role") == "tool" for m in messages):
            return StreamingResponse(
                iter([_sse(_content_events("Gotovo, alat je odgovorio."))]),
                media_type="text/event-stream",
            )
        if "SAT" in last_user.upper():
            return StreamingResponse(iter([_sse(_tool_events())]), media_type="text/event-stream")
        if "SPORO" in last_user.upper():

            async def slow():
                for i in range(30):
                    evt = {"choices": [{"delta": {"content": f"Deo {i}. "}}]}
                    yield f"data: {json.dumps(evt)}\n\n"
                    await asyncio.sleep(0.15)
                yield f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(slow(), media_type="text/event-stream")
        return StreamingResponse(
            iter([_sse(_content_events("Zdravo iz lažnog modela."))]),
            media_type="text/event-stream",
        )

    return app


@pytest.fixture
def fake_llm_url(free_port: int):
    app = _build_fake_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=free_port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("fake LLM server did not start")
        time.sleep(0.02)
    yield f"http://127.0.0.1:{free_port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _patched_llm_settings(fake_llm_url: str, monkeypatch: pytest.MonkeyPatch):
    fake_llm = dataclasses.replace(
        SETTINGS.llm,
        provider="bailian",
        base_url=fake_llm_url,
        api_key="test-key",
        model="fake-model",
        thinking="disabled",
    )
    fake_settings = dataclasses.replace(SETTINGS, llm=fake_llm)
    monkeypatch.setattr(llm_mod, "SETTINGS", fake_settings)
    monkeypatch.setattr(llm_mod, "_client", None)
    yield


@pytest.fixture(autouse=True)
def _silenced_audio(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    async def fake_synthesize(text: str, *args: Any, **kwargs: Any) -> str:
        p = tmp_path / f"tts_{uuid.uuid4().hex}.wav"
        p.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
        return str(p)

    async def fake_play_file(path: Any) -> None:
        return None

    async def fake_stop() -> None:
        return None

    monkeypatch.setattr(tts_mod, "synthesize", fake_synthesize)
    monkeypatch.setattr(player_mod, "play_file", fake_play_file)
    monkeypatch.setattr(player_mod, "stop", fake_stop)
    yield


@pytest.fixture(autouse=True)
def _no_world_state(monkeypatch: pytest.MonkeyPatch):
    async def fake_world_state() -> str:
        return ""

    monkeypatch.setattr(agent_loop, "build_world_state", fake_world_state)
    yield


@pytest.fixture
def store(tmp_path: Path) -> PermissionStore:
    s = PermissionStore(tmp_path / "permissions.json")
    s.set_default("allow")
    return s


@pytest.fixture
def clean_sessions(tmp_data_dir):
    agent_loop.SESSIONS.clear()
    yield
    for sess in list(agent_loop.SESSIONS.values()):
        speech_mod.SPEECH.discard(sess.id)
    agent_loop.SESSIONS.clear()


async def _wait_idle(session_id: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        sess = agent_loop.SESSIONS.get(session_id)
        if sess is not None and not sess.busy and sess.queue.empty():
            await asyncio.sleep(0.05)
            if not sess.busy and sess.queue.empty():
                return
        await asyncio.sleep(0.02)
    raise TimeoutError(f"session {session_id} still busy")


async def _collect_events() -> tuple[asyncio.Queue, Any]:
    q = await BUS.subscribe()
    return q, None


@pytest.mark.asyncio
async def test_chat_turn_streams_and_persists(store: PermissionStore, clean_sessions) -> None:
    q, _ = await _collect_events()
    sid = await agent_loop.chat("Koliko je 2+2?", store=store)
    await _wait_idle(sid)

    sess = agent_loop.SESSIONS[sid]
    assert [m["role"] for m in sess.messages] == ["user", "assistant"]
    assert sess.messages[1]["content"] == "Zdravo iz lažnog modela."

    kinds: list[str] = []
    deltas = ""
    while not q.empty():
        evt = json.loads(q.get_nowait())
        kinds.append(evt["kind"])
        if evt["kind"] == "assistant_delta":
            deltas += evt["payload"]["text"]
    assert "assistant_start" in kinds
    assert "assistant_done" in kinds
    assert deltas == "Zdravo iz lažnog modela."
    BUS.unsubscribe(q)


@pytest.mark.asyncio
async def test_ptt_chat_turn_marks_server_side_speech(store: PermissionStore, clean_sessions) -> None:
    q, _ = await _collect_events()
    sid = await agent_loop.chat("PTT odgovor", store=store, source="ptt")
    await _wait_idle(sid)
    await asyncio.sleep(0.05)

    tts_events: list[dict[str, Any]] = []
    while not q.empty():
        evt = json.loads(q.get_nowait())
        if evt["kind"] == "tts_speak":
            tts_events.append(evt["payload"])

    assert tts_events
    assert all(event["server_played"] is True for event in tts_events)
    BUS.unsubscribe(q)


@pytest.mark.asyncio
async def test_tool_call_loop(store: PermissionStore, clean_sessions) -> None:
    sid = await agent_loop.chat("KOLIKO JE SAT?", store=store)
    await _wait_idle(sid)

    sess = agent_loop.SESSIONS[sid]
    roles = [m["role"] for m in sess.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    tool_msg = sess.messages[2]
    assert tool_msg["name"] == "time_now"
    assert tool_msg["tool_call_id"] == "call_t1"
    assert "iso" in json.loads(tool_msg["content"])
    assert sess.messages[3]["content"] == "Gotovo, alat je odgovorio."


@pytest.mark.asyncio
async def test_local_tool_loop_then_cloud_preserves_canonical_history(
    store: PermissionStore,
    clean_sessions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadyToolsRunner:
        def is_ready(self, model_id: str) -> bool:
            return model_id == "fake:model"

        async def capability_for_model(self, model_id: str) -> str:
            return "tools"

    monkeypatch.setattr(local_models, "RUNNER", ReadyToolsRunner())
    captured: list[tuple[str | None, list[dict[str, Any]] | None, list[dict[str, Any]]]] = []

    async def fake_stream_clean(
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ):
        captured.append((model, tools, messages))
        if len(captured) == 1:
            yield "finish", "tool_calls"
            yield (
                "done",
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "local-call-1",
                            "type": "function",
                            "function": {"name": "time_now", "arguments": "{}"},
                        }
                    ],
                    "finish_reason": "tool_calls",
                },
            )
            return
        yield "delta", "Gotovo."
        yield "finish", "stop"
        yield "done", {"role": "assistant", "content": "Gotovo.", "tool_calls": [], "finish_reason": "stop"}

    monkeypatch.setattr(agent_loop, "stream_clean", fake_stream_clean)

    sid = await agent_loop.chat("KOLIKO JE SAT?", model="local:fake:model", store=store)
    await _wait_idle(sid)

    sess = agent_loop.SESSIONS[sid]
    assert [message["role"] for message in sess.messages] == ["user", "assistant", "tool", "assistant"]
    assert sess.messages[1]["tool_calls"][0]["id"] == "local-call-1"
    assert captured[0][0] == "local:fake:model"
    assert captured[0][1]
    assert any(message.get("role") == "tool" for message in captured[1][2])

    await agent_loop.chat("Nastavi preko clouda", session_id=sid, store=store, model="fake-cloud")
    await _wait_idle(sid)

    assert captured[2][0] == "fake-cloud"
    cloud_history = captured[2][2]
    assert any(message.get("tool_calls") for message in cloud_history)
    assert any(
        message.get("role") == "tool" and message.get("tool_call_id") == "local-call-1"
        for message in cloud_history
    )


@pytest.mark.asyncio
async def test_local_not_ready_preflight_does_not_fallback_to_cloud(
    store: PermissionStore,
    clean_sessions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NotReadyRunner:
        def is_ready(self, model_id: str) -> bool:
            return False

    called = False

    async def should_not_stream(*args: Any, **kwargs: Any):
        nonlocal called
        called = True
        raise AssertionError("a not-ready local turn must not reach any model")
        yield "done", {}

    monkeypatch.setattr(local_models, "RUNNER", NotReadyRunner())
    monkeypatch.setattr(agent_loop, "stream_clean", should_not_stream)

    with pytest.raises(local_models.LocalModelNotReadyError):
        await agent_loop.chat("ne šalji ovo", store=store, model="local:fake:model")

    assert called is False
    assert agent_loop.SESSIONS == {}


@pytest.mark.asyncio
async def test_runtime_local_failure_falls_back_without_changing_preference(
    store: PermissionStore,
    clean_sessions,
    tmp_data_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadyRunner:
        def is_ready(self, model_id: str) -> bool:
            return model_id == "fake:model"

        async def capability_for_model(self, model_id: str) -> str:
            return "tools"

    calls: list[str | None] = []

    async def fake_stream_clean(
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ):
        calls.append(model)
        if len(calls) == 1:
            raise RuntimeError("local stream disconnected")
        yield "delta", "cloud recovery"
        yield "finish", "stop"
        yield "done", {"role": "assistant", "content": "cloud recovery", "tool_calls": []}

    monkeypatch.setattr(local_models, "RUNNER", ReadyRunner())
    monkeypatch.setattr(agent_loop, "stream_clean", fake_stream_clean)
    await state_store.set_state_value("ui", {"model": "local:fake:model"})

    q = await BUS.subscribe()
    sid = await agent_loop.chat("oprezno", store=store, model="local:fake:model")
    await _wait_idle(sid)

    events = []
    while not q.empty():
        events.append(json.loads(q.get_nowait()))
    BUS.unsubscribe(q)

    fallbacks = [event["payload"] for event in events if event["kind"] == "model_fallback"]
    assert calls == ["local:fake:model", None]
    assert len(fallbacks) == 1
    assert fallbacks[0]["model"] == "local:fake:model"
    assert fallbacks[0]["fallback_model"] == SETTINGS.llm.model
    assert fallbacks[0]["stage"] == "runtime"
    assert await state_store.get_state_value("ui") == {"model": "local:fake:model"}
    assert agent_loop.SESSIONS[sid].messages[-1]["content"] == "cloud recovery"


@pytest.mark.asyncio
async def test_fifo_three_quick_messages(store: PermissionStore, clean_sessions) -> None:
    sid = await agent_loop.chat("prva poruka", store=store)
    await agent_loop.chat("druga poruka", session_id=sid, store=store)
    await agent_loop.chat("treća poruka", session_id=sid, store=store)
    await _wait_idle(sid, timeout=20)

    sess = agent_loop.SESSIONS[sid]
    assert [m["role"] for m in sess.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert [m["content"] for m in sess.messages[::2]] == [
        "prva poruka",
        "druga poruka",
        "treća poruka",
    ]


@pytest.mark.asyncio
async def test_barge_in_repairs_history(store: PermissionStore, clean_sessions) -> None:
    q, _ = await _collect_events()
    sid = await agent_loop.chat("SPORO molim te", store=store)

    saw_delta = False
    deadline = time.time() + 10
    while time.time() < deadline and not saw_delta:
        try:
            evt = json.loads(await asyncio.wait_for(q.get(), timeout=1))
            saw_delta = evt["kind"] == "assistant_delta"
        except TimeoutError:
            continue
    assert saw_delta, "streaming should have started before barge-in"

    await agent_loop.stop(sid)
    await _wait_idle(sid)

    sess = agent_loop.SESSIONS[sid]
    for i, m in enumerate(sess.messages):
        if m.get("role") == "tool":
            assert i > 0 and sess.messages[i - 1].get("tool_calls"), "orphan tool message"
    for m in sess.messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            call_ids = {tc["id"] for tc in m["tool_calls"]}
            answered = {t.get("tool_call_id") for t in sess.messages if t.get("role") == "tool"}
            assert call_ids <= answered, "tool_calls without results after cancel"

    sid2 = await agent_loop.chat("Nastavi normalno", session_id=sid, store=store)
    await _wait_idle(sid2)
    sess = agent_loop.SESSIONS[sid]
    assert sess.messages[-1]["role"] == "assistant"
    assert sess.messages[-1]["content"] == "Zdravo iz lažnog modela."
    BUS.unsubscribe(q)
