"""Tool-calling agent loop with per-session turn orchestration.

Every session owns a FIFO queue of user turns processed by a single worker,
so rapid consecutive messages can never interleave or corrupt the history.
A turn can be cancelled mid-flight (barge-in): the LLM stream is closed,
running tool work is interrupted, and the history is repaired so the next
turn always sees a valid message sequence.

Everything is streamed to the event bus; spoken output is driven by the
server-side speech scheduler (`jarvis/audio/speech.py`), which starts
synthesizing sentence-by-sentence while the model is still generating.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .. import permissions as perm_mod
from ..audio.speech import SPEECH
from ..bus import BUS
from ..config import SETTINGS
from ..context import build_world_state
from ..llm import LLMError, stream_clean
from ..tools import DEFAULT_REGISTRY, ToolErrorCode, ToolExecutionContext, ToolExecutor, ToolResult
from .prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_NOTOOLS

MAX_HISTORY_MESSAGES = 80

TOOL_EXECUTOR = ToolExecutor(DEFAULT_REGISTRY, speech_suppressor=SPEECH.suppress)


def _collapse_double(text: str) -> str:
    """MiniMax-M3 occasionally emits the same text twice; collapse exact doubles."""
    if len(text) >= 4 and len(text) % 2 == 0:
        half = len(text) // 2
        if text[:half] == text[half:]:
            return text[:half]
    return text


def _drop_orphans(messages: list[dict[str, Any]]) -> None:
    """Strip leading `tool` results and orphaned `assistant(tool_calls)`
    messages so the next iteration of the OpenAI-compatible API doesn't
    reject the sequence with HTTP 400. Persisted sessions from older code
    paths could contain such orphans."""
    while messages and (
        messages[0].get("role") == "tool"
        or (messages[0].get("role") == "assistant" and messages[0].get("tool_calls"))
    ):
        del messages[0]


def _trim_history(messages: list[dict[str, Any]]) -> None:
    """Keep the history bounded. Cuts only at user-message boundaries so an
    assistant tool_calls message is never separated from its tool results,
    and drops any leading orphan tool blocks first."""
    _drop_orphans(messages)
    while len(messages) > MAX_HISTORY_MESSAGES:
        cut = None
        for i in range(1, len(messages)):
            if messages[i].get("role") == "user":
                cut = i
                break
        if cut is None:
            del messages[:1]
            _drop_orphans(messages)
        else:
            del messages[:cut]


@dataclass
class _TurnRequest:
    text: str
    model: str | None = None
    source: str = "text"


@dataclass
class Session:
    id: str
    title: str = "Nova konverzacija"
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    worker: asyncio.Task | None = None
    turn_task: asyncio.Task | None = None

    @property
    def busy(self) -> bool:
        return self.turn_task is not None and not self.turn_task.done()

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "messages": len(self.messages),
            "busy": self.busy,
            "queued": self.queue.qsize(),
        }


SESSIONS: dict[str, Session] = {}

SESSIONS_FILE = SETTINGS.data_dir / "sessions.json"


def load_sessions() -> None:
    """Restore persisted sessions into memory on startup (best-effort)."""
    try:
        raw = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for item in raw.get("sessions") or []:
        sid = item.get("id")
        if not sid or sid in SESSIONS:
            continue
        SESSIONS[sid] = Session(
            id=sid,
            title=item.get("title") or "Nova konverzacija",
            messages=item.get("messages") or [],
            created_at=float(item.get("created_at") or time.time()),
        )


async def save_sessions() -> None:
    """Atomically persist all sessions to data/sessions.json."""
    payload = {
        "saved_at": time.time(),
        "sessions": [
            {
                "id": s.id,
                "title": s.title,
                "created_at": s.created_at,
                "messages": s.messages,
            }
            for s in SESSIONS.values()
        ],
    }

    def _write() -> None:
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SESSIONS_FILE.with_name(SESSIONS_FILE.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(SESSIONS_FILE)

    try:
        await asyncio.to_thread(_write)
    except OSError:
        pass


def get_or_create(session_id: str | None = None) -> Session:
    sid = session_id or uuid.uuid4().hex[:12]
    sess = SESSIONS.get(sid)
    if sess is None:
        sess = Session(id=sid)
        SESSIONS[sid] = sess
    return sess


def list_sessions() -> list[dict[str, Any]]:
    return [s.snapshot() for s in SESSIONS.values()]


async def reset_session(session_id: str) -> None:
    sess = SESSIONS.get(session_id)
    if sess is None:
        return
    _cancel_turn(sess)
    _drain_queue(sess)
    SPEECH.discard(sess.id)
    sess.messages.clear()
    sess.title = "Nova konverzacija"
    await save_sessions()


async def delete_session(session_id: str) -> bool:
    """Drop the session entirely: cancel in-flight work, free its speech
    buffer, remove from the in-memory map and persisted store. Returns
    False if no such session existed."""
    sess = SESSIONS.get(session_id)
    if sess is None:
        return False
    _cancel_turn(sess)
    _drain_queue(sess)
    await SPEECH.cancel(sess.id)
    SESSIONS.pop(session_id, None)
    await save_sessions()
    return True


async def chat(
    user_text: str,
    *,
    session_id: str | None = None,
    store: perm_mod.PermissionStore,
    model: str | None = None,
    interrupt: bool = False,
    source: str = "text",
) -> str:
    """Enqueue a user turn for the session and return the session id.

    With ``interrupt=True`` (voice barge-in) the in-flight turn and any
    queued turns are dropped before the new one starts. With
    ``interrupt=False`` the turn is appended to the session's FIFO queue,
    so rapid consecutive messages are processed in order, never lost.
    """
    await _ensure_local_model_ready(model)
    sess = get_or_create(session_id)
    if interrupt:
        _cancel_turn(sess)
        _drain_queue(sess)
        await SPEECH.cancel(sess.id)
    turn_source = "ptt" if source == "ptt" else "text"
    sess.queue.put_nowait(_TurnRequest(text=user_text, model=model, source=turn_source))
    _ensure_worker(sess, store)
    await _publish_busy(sess)
    return sess.id


async def stop(session_id: str) -> bool:
    """Cancel the session's in-flight turn and drop queued turns."""
    sess = SESSIONS.get(session_id)
    if sess is None:
        return False
    had_work = sess.busy or sess.queue.qsize() > 0
    _cancel_turn(sess)
    _drain_queue(sess)
    await SPEECH.cancel(sess.id)
    await _publish_busy(sess)
    return had_work


def _drain_queue(sess: Session) -> None:
    while not sess.queue.empty():
        try:
            sess.queue.get_nowait()
        except asyncio.QueueEmpty:
            break


def _cancel_turn(sess: Session) -> None:
    if sess.turn_task is not None and not sess.turn_task.done():
        sess.turn_task.cancel()


async def _publish_busy(sess: Session) -> None:
    await BUS.publish(
        "session_busy",
        {"session": sess.id, "busy": sess.busy, "queued": sess.queue.qsize()},
    )


def _ensure_worker(sess: Session, store: perm_mod.PermissionStore) -> None:
    if sess.worker is not None and not sess.worker.done():
        return
    sess.worker = asyncio.create_task(_session_worker(sess, store))


async def _session_worker(sess: Session, store: perm_mod.PermissionStore) -> None:
    try:
        while True:
            try:
                req: _TurnRequest = await asyncio.wait_for(sess.queue.get(), timeout=60)
            except TimeoutError:
                if sess.queue.empty():
                    return
                continue
            sess.turn_task = asyncio.create_task(
                run_turn(sess, req.text, model=req.model, source=req.source, store=store)
            )
            try:
                await sess.turn_task
            except asyncio.CancelledError:
                _repair_after_cancel(sess)
                await BUS.publish("assistant_cancelled", {"session": sess.id})
            except Exception as exc:  # noqa: BLE001
                from ..local_models import LocalModelNotReadyError

                if isinstance(exc, LocalModelNotReadyError):
                    await BUS.publish(
                        "assistant_error",
                        {
                            "session": sess.id,
                            "error_code": exc.code,
                            "error": str(exc),
                        },
                    )
                else:
                    await BUS.publish("assistant_error", {"session": sess.id, "error": repr(exc)})
            finally:
                sess.turn_task = None
                await _publish_busy(sess)
                await save_sessions()
    finally:
        sess.worker = None


def _repair_after_cancel(sess: Session) -> None:
    """Append synthetic tool results when a turn was cancelled mid-tool so
    the history stays a valid OpenAI message sequence."""
    msgs = sess.messages
    if not msgs:
        return
    last = msgs[-1]
    pending_ids: list[tuple[str, str]] = []
    if last.get("role") == "assistant" and last.get("tool_calls"):
        for tc in last["tool_calls"]:
            pending_ids.append((tc.get("id") or "", (tc.get("function") or {}).get("name") or ""))
    elif last.get("role") == "tool":
        for i in range(len(msgs) - 1, -1, -1):
            m = msgs[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                done = {mm.get("tool_call_id") for mm in msgs[i + 1 :] if mm.get("role") == "tool"}
                for tc in m["tool_calls"]:
                    if tc.get("id") not in done:
                        pending_ids.append((tc.get("id") or "", (tc.get("function") or {}).get("name") or ""))
                break
    for call_id, name in pending_ids:
        msgs.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": ToolResult.failure(ToolErrorCode.CANCELLED, "cancelled by user").to_json(),
            }
        )


async def _fallback_to_cloud(session: Session, local_model: str, exc: Exception) -> None:
    """Local model failed mid-turn: silence any partial speech, announce the
    fallback on the bus, and let the caller retry with the cloud model."""
    await SPEECH.cancel(session.id)
    await BUS.publish(
        "model_fallback",
        {
            "session": session.id,
            "model": local_model,
            "requested_model": local_model,
            "fallback_model": SETTINGS.llm.model,
            "reason": str(exc)[:300],
            "stage": "runtime",
        },
    )


async def _ensure_local_model_ready(model: str | None) -> None:
    """Reject explicit local requests before a turn can fall back to cloud."""

    if not model or not model.startswith("local:"):
        return
    from ..local_models import RUNNER, LocalModelNotReadyError

    local_id = model[len("local:") :]
    if not RUNNER.is_ready(local_id):
        status_reader = getattr(RUNNER, "astatus", None)
        if callable(status_reader):
            status = await status_reader()
        else:
            sync_status = getattr(RUNNER, "status", None)
            status = sync_status() if callable(sync_status) else {}
        state = status.get("state") if isinstance(status, dict) else None
        detail = f"lokalni model {local_id!r} nije spreman za chat"
        if state:
            detail += f" (stanje: {state})"
        raise LocalModelNotReadyError(detail)


async def run_turn(
    session: Session,
    user_text: str,
    *,
    model: str | None = None,
    source: str = "text",
    max_iterations: int = 8,
    store: perm_mod.PermissionStore,
) -> str:
    """Drive a single user turn. Streams everything to the bus and feeds the
    speech scheduler. Returns the final assistant text."""
    await _ensure_local_model_ready(model)
    session.messages.append({"role": "user", "content": user_text})
    if len(session.messages) == 1 and not session.title.startswith("✦"):
        session.title = user_text.strip().split("\n", 1)[0][:60]
        await BUS.publish("session_update", {"id": session.id, "title": session.title})
    _trim_history(session.messages)
    await save_sessions()

    SPEECH.begin_turn(session.id, source=source)
    try:
        world_state = await build_world_state()
    except Exception:  # noqa: BLE001
        world_state = ""

    active_model = model
    capability: str | None = None
    if active_model and active_model.startswith("local:"):
        from ..local_models import RUNNER

        local_id = active_model[len("local:") :]
        capability = await RUNNER.capability_for_model(local_id)

    def _system_content_for(mdl: str | None) -> str:
        prompt = SYSTEM_PROMPT_NOTOOLS if (capability == "notools" and mdl) else SYSTEM_PROMPT
        return prompt + (f"\n\n{world_state}" if world_state else "")

    iteration = 0
    final_text = ""
    cancelled = False
    try:
        while iteration < max_iterations:
            iteration += 1
            msgs = [{"role": "system", "content": _system_content_for(active_model)}] + session.messages
            await BUS.publish(
                "assistant_start",
                {"session": session.id, "iteration": iteration, "model": active_model},
            )
            try:
                full_text = ""
                tool_calls: list[dict[str, Any]] = []
                finish_reason: str | None = None
                model_tools = TOOL_EXECUTOR.registry.schemas()
                if active_model and active_model.startswith("local:") and capability == "notools":
                    model_tools = None
                async for kind, value in stream_clean(msgs, model=active_model, tools=model_tools):
                    if kind == "delta":
                        full_text += value
                        SPEECH.feed(session.id, value)
                        await BUS.publish("assistant_delta", {"session": session.id, "text": value})
                    elif kind == "reasoning":
                        await BUS.publish("reasoning_delta", {"session": session.id, "text": value})
                    elif kind == "finish":
                        finish_reason = str(value)
                    elif kind == "done":
                        tool_calls = value.get("tool_calls") or []
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": _collapse_double(full_text),
                }
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                session.messages.append(assistant_msg)
                final_text = assistant_msg["content"]
                SPEECH.end_message(session.id)
                await BUS.publish(
                    "assistant_done",
                    {
                        "session": session.id,
                        "finish": finish_reason,
                        "has_tools": bool(tool_calls),
                        "final": not tool_calls,
                    },
                )
                if not tool_calls:
                    return final_text
                for tc in tool_calls:
                    await _execute_tool(session, tc, store)
            except asyncio.CancelledError:
                cancelled = True
                raise
            except Exception as exc:
                from ..local_models import LocalModelNotReadyError

                if isinstance(exc, LocalModelNotReadyError):
                    await BUS.publish(
                        "assistant_error",
                        {
                            "session": session.id,
                            "error_code": exc.code,
                            "error": str(exc),
                        },
                    )
                    return final_text
                if active_model and active_model.startswith("local:") and iteration < max_iterations:
                    await _fallback_to_cloud(session, active_model, exc)
                    active_model = None
                    continue
                error_text = str(exc) if isinstance(exc, LLMError) else repr(exc)
                await BUS.publish("assistant_error", {"session": session.id, "error": error_text})
                return final_text

        await BUS.publish(
            "assistant_done",
            {"session": session.id, "finish": "max_iterations", "has_tools": False, "final": True},
        )
        return final_text
    finally:
        if cancelled:
            # Drop any partial sentence buffered so far — a barge-in must not
            # speak a half-finished thought. The caller owns tts_stop/player.
            SPEECH.discard(session.id)
        else:
            SPEECH.end_turn(session.id)


async def _execute_tool(session: Session, tc: dict[str, Any], store: perm_mod.PermissionStore) -> None:
    execution = await TOOL_EXECUTOR.execute_call(
        tc,
        context=ToolExecutionContext(session_id=session.id, permission_store=store),
    )
    session.messages.append(execution.tool_message())
