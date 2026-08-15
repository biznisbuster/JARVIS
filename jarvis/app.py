"""FastAPI server: REST endpoints + WebSocket event stream + static UI."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import state as runtime_state
from .agent import loop as agent_loop
from .bus import BUS
from .config import ROOT, SETTINGS

ROOT_PATH = ROOT
WEB_DIR = ROOT / "web"
WEB_UI_DIR = ROOT / "web-ui"
WEB_UI_DIST = WEB_UI_DIR / "dist"
LOGS_DIR = ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)


def _active_ui_dir() -> Path | None:
    """Prefer the Vite production build at `web-ui/dist` if it exists;
    otherwise fall back to the legacy vanilla `web/` directory. Both can
    be absent during early scaffolding (dev mode uses Vite's own server)."""
    if WEB_UI_DIST.is_dir() and (WEB_UI_DIST / "index.html").exists():
        return WEB_UI_DIST
    if WEB_DIR.exists():
        return WEB_DIR
    return None

permission_store = runtime_state.permission_store


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .log import setup_logging

    setup_logging()
    agent_loop.load_sessions()
    BUS.attach(asyncio.get_running_loop())
    await BUS.publish("status", {"ready": True, "ts": time.time()})
    # Restore an existing dedicated YTM browser profile if one was connected
    # before. A fresh install remains DISCONNECTED until the user clicks the
    # explicit Connect YouTube Music action in the UI.
    try:
        from .media import ytm_web as _ytm_web

        _ytm_web.warm_up()
    except Exception:
        pass
    # Best-effort: warm up the STT model in the background so the first
    # voice message doesn't pay the model-load cost.
    try:
        from .audio import stt as stt_mod

        asyncio.create_task(stt_mod.warmup())
    except Exception:
        pass
    # Best-effort: start global push-to-talk listener. If Accessibility
    # permission is missing, the user enables it from the UI tab and we
    # surface the error. Either way, Jarvis keeps running.
    try:
        from .hotkey import PTT

        if SETTINGS.audio.push_to_talk.enabled:
            PTT.enable()
            await BUS.publish("ptt_status", PTT.status())
    except Exception as exc:
        await BUS.publish("ptt_status", {"enabled": False, "error": str(exc)})
    yield
    # Close the dedicated YT Music context cleanly while retaining its
    # persistent on-device profile for the next server start.
    try:
        from .media import ytm_web as _ytm_web

        await _ytm_web.shutdown()
    except Exception:
        pass
    # Teardown: stop the listener so we don't leave a key tap dangling.
    try:
        from .hotkey import PTT

        PTT.disable()
    except Exception:
        pass


app = FastAPI(title="Jarvis", version="0.1.0", lifespan=lifespan)


# ---- REST: chat & sessions -------------------------------------------------


class ChatIn(BaseModel):
    text: str
    session_id: str | None = None
    model: str | None = None
    interrupt: bool = False
    source: str = "text"


class ChatOut(BaseModel):
    session_id: str


@app.post("/api/chat", response_model=ChatOut)
async def api_chat(body: ChatIn) -> ChatOut:
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    sid = await agent_loop.chat(
        text,
        session_id=body.session_id,
        store=permission_store,
        model=body.model,
        interrupt=body.interrupt,
        source=body.source,
    )
    return ChatOut(session_id=sid)


class ChatStopIn(BaseModel):
    session_id: str


@app.post("/api/chat/stop")
async def api_chat_stop(body: ChatStopIn) -> JSONResponse:
    stopped = await agent_loop.stop(body.session_id)
    return JSONResponse({"ok": True, "stopped": stopped})


@app.get("/api/sessions")
async def api_sessions() -> JSONResponse:
    return JSONResponse(agent_loop.list_sessions())


@app.post("/api/sessions", status_code=201)
async def api_sessions_create() -> JSONResponse:
    """Create an empty session (no greeting message). Useful for `New chat`
    buttons so the assistant doesn't burn tokens on a token-greeting."""
    sess = agent_loop.get_or_create()
    await agent_loop.save_sessions()
    return JSONResponse({"id": sess.id, "title": sess.title, "created_at": sess.created_at})


@app.delete("/api/sessions/{session_id}")
async def api_sessions_delete(session_id: str) -> JSONResponse:
    ok = await agent_loop.delete_session(session_id)
    if not ok:
        raise HTTPException(404, "session not found")
    return JSONResponse({"ok": True, "id": session_id})


@app.post("/api/sessions/{session_id}/reset")
async def api_reset(session_id: str) -> JSONResponse:
    await agent_loop.reset_session(session_id)
    return JSONResponse({"ok": True})


@app.get("/api/sessions/{session_id}")
async def api_session(session_id: str) -> JSONResponse:
    sess = agent_loop.SESSIONS.get(session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    return JSONResponse(
        {
            "id": sess.id,
            "title": sess.title,
            "created_at": sess.created_at,
            "messages": sess.messages,
        }
    )


# ---- REST: permissions -----------------------------------------------------


class PermissionsIn(BaseModel):
    default_policy: str | None = None
    tools: dict[str, str] | None = None


@app.get("/api/permissions")
async def api_permissions_get() -> JSONResponse:
    return JSONResponse(permission_store.snapshot())


@app.put("/api/permissions")
async def api_permissions_put(body: PermissionsIn) -> JSONResponse:
    if body.default_policy:
        permission_store.set_default(body.default_policy)
    if body.tools:
        for k, v in body.tools.items():
            permission_store.set_policy(k, v)
    await BUS.publish("permissions_changed", permission_store.snapshot())
    return JSONResponse(permission_store.snapshot())


class PermissionResolveIn(BaseModel):
    request_id: str
    action: str  # "allow" | "deny"
    remember: bool = False


@app.post("/api/permissions/resolve")
async def api_permissions_resolve(body: PermissionResolveIn) -> JSONResponse:
    ok = permission_store.resolve(body.request_id, body.action, body.remember)
    return JSONResponse({"ok": ok})


@app.get("/api/permissions/pending")
async def api_permissions_pending() -> JSONResponse:
    return JSONResponse(permission_store.list_pending())


# ---- REST: connections / status --------------------------------------------


@app.get("/api/ytm/connection")
async def api_ytm_connection() -> JSONResponse:
    from .media import ytm_web

    return JSONResponse(await ytm_web.connection_status())


@app.post("/api/ytm/connect")
async def api_ytm_connect() -> JSONResponse:
    from .media import ytm_web

    return JSONResponse(await ytm_web.connect())


@app.get("/api/connections")
async def api_connections() -> JSONResponse:
    return JSONResponse(await _connections_payload())


async def _connections_payload() -> dict:
    from .audio import tts as tts_mod
    from .audio.focus import FOCUS
    from .hotkey import PTT
    from .media import ytm_web

    voice_info = await asyncio.to_thread(tts_mod.current_voice_info)
    return {
        "llm": {
            "provider": SETTINGS.llm.provider,
            "base_url": SETTINGS.llm.base_url,
            "model": SETTINGS.llm.model,
            "small_model": SETTINGS.llm.small_model,
            "api_key_set": bool(SETTINGS.llm.api_key),
        },
        "kilo": {
            "bin": SETTINGS.kilo.bin,
            "available": bool(shutil.which(SETTINGS.kilo.bin)),
            "config_path": str(SETTINGS.kilo.config_path),
            "config_exists": SETTINGS.kilo.config_path.exists(),
        },
        "whisper": {
            "backend": SETTINGS.whisper.backend,
            "model": SETTINGS.whisper.model,
            "device": SETTINGS.whisper.device,
            "compute": SETTINGS.whisper.compute,
            "loaded": runtime_state.whisper_state.model is not None,
        },
        "tts": {
            "backend": SETTINGS.tts.backend,
            "output": SETTINGS.audio.output,
            "active": voice_info,
            "piper": {
                "voice": SETTINGS.piper.voice,
                "length_scale": SETTINGS.piper.length_scale,
                "say_voice": SETTINGS.piper.say_voice,
                "loaded": runtime_state.piper_state.model is not None,
            },
            "xtts": {
                "model": SETTINGS.tts.xtts.model,
                "language": SETTINGS.tts.xtts.language,
                "use_gpu": SETTINGS.tts.xtts.use_gpu,
                "speaker_wav": str(SETTINGS.tts.xtts.speaker_wav),
                "loaded": runtime_state.xtts_state.model is not None,
            },
        },
        "ptt": PTT.status(),
        "listen": FOCUS.status(),
        "ytm": await ytm_web.connection_status(),
    }


@app.get("/api/status")
async def api_status() -> JSONResponse:
    return JSONResponse({"ready": True, "ts": time.time(), "version": "0.1.0"})


@app.get("/api/models")
async def api_models() -> JSONResponse:
    from .local_models import RUNNER

    # Every discovered local model is offered in the dropdown (loaded or
    # not); the frontend triggers a load when the user picks one that is
    # not in RAM yet. `local:` prefix lets the LLM router tell them apart.
    local = []
    for m in await RUNNER.discover():
        label = f"{m['label']} (lokalni"
        if m.get("capability") == "notools":
            label += ", bez tool-ova"
        label += ")"
        local.append({"id": f"local:{m['id']}", "label": label})
    return JSONResponse(
        {
            "provider": SETTINGS.llm.provider,
            "current": SETTINGS.llm.model,
            "small": SETTINGS.llm.small_model,
            "available": SETTINGS.llm.models + local,
        }
    )


# ---- Persistent UI state (active model, TTS toggle) -------------------------


class UIStateIn(BaseModel):
    model: str | None = None
    tts_enabled: bool | None = None


@app.get("/api/state")
async def api_state_get() -> JSONResponse:
    from . import state_store

    ui = await state_store.get_state_value("ui", {})
    return JSONResponse({"ui": ui if isinstance(ui, dict) else {}})


@app.put("/api/state")
async def api_state_put(body: UIStateIn) -> JSONResponse:
    from . import state_store

    ui = await state_store.get_state_value("ui", {})
    if not isinstance(ui, dict):
        ui = {}
    if body.model is not None:
        ui["model"] = body.model
    if body.tts_enabled is not None:
        ui["tts_enabled"] = body.tts_enabled
    await state_store.set_state_value("ui", ui)
    return JSONResponse({"ok": True, "ui": ui})


# ---- Local model management (discover/load/unload/pull) ---------------------


@app.get("/api/local_models")
async def api_local_models() -> JSONResponse:
    from .local_models import RUNNER

    return JSONResponse(
        {
            "runner": await RUNNER.astatus(),
            "models": await RUNNER.discover(),
            "pulls": RUNNER.pulls_status(),
        }
    )


class LocalModelIdIn(BaseModel):
    model_id: str


class LocalModelTagIn(BaseModel):
    tag: str


@app.post("/api/local_models/load")
async def api_local_models_load(body: LocalModelIdIn) -> JSONResponse:
    from .local_models import RUNNER

    try:
        status = await RUNNER.load(body.model_id)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "runner": status})


@app.post("/api/local_models/unload")
async def api_local_models_unload() -> JSONResponse:
    from .local_models import RUNNER

    status = await RUNNER.unload()
    return JSONResponse({"ok": True, "runner": status})


@app.post("/api/local_models/pull")
async def api_local_models_pull(body: LocalModelTagIn) -> JSONResponse:
    from .local_models import RUNNER

    try:
        res = await RUNNER.start_pull(body.tag)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse(res)


@app.post("/api/local_models/pull/cancel")
async def api_local_models_pull_cancel(body: LocalModelTagIn) -> JSONResponse:
    from .local_models import RUNNER

    res = await RUNNER.cancel_pull(body.tag)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


# ---- Global push-to-talk ---------------------------------------------------


@app.get("/api/ptt")
async def api_ptt_get() -> JSONResponse:
    from .hotkey import PTT

    return JSONResponse(PTT.status())


@app.post("/api/ptt/enable")
async def api_ptt_enable() -> JSONResponse:
    from .hotkey import PTT

    try:
        return JSONResponse({"ok": True, "ptt": PTT.enable()})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc), "ptt": PTT.status()}, status_code=400)


@app.post("/api/ptt/disable")
async def api_ptt_disable() -> JSONResponse:
    from .hotkey import PTT

    return JSONResponse({"ok": True, "ptt": PTT.disable()})


# ---- Listen mode (audio focus) --------------------------------------------


class ListenIn(BaseModel):
    reason: str = "browser"


@app.get("/api/audio/listen")
async def api_listen_get() -> JSONResponse:
    from .audio.focus import FOCUS

    return JSONResponse(FOCUS.status())


@app.post("/api/audio/listen/start")
async def api_listen_start(body: ListenIn) -> JSONResponse:
    from .audio.focus import FOCUS

    snap = await FOCUS.enter(body.reason or "browser")
    return JSONResponse({"ok": True, "focus": snap})


@app.post("/api/audio/listen/stop")
async def api_listen_stop(body: ListenIn) -> JSONResponse:
    from .audio.focus import FOCUS

    snap = await FOCUS.exit(body.reason or "browser")
    return JSONResponse({"ok": True, "focus": snap})


# ---- REST: audio -----------------------------------------------------------


class TTSIn(BaseModel):
    text: str


@app.get("/api/tts/voices")
async def api_tts_voices() -> JSONResponse:
    from .audio import tts as tts_mod

    voices = await asyncio.to_thread(tts_mod.list_voices)
    info = tts_mod.current_voice_info()
    return JSONResponse({"voices": voices, **info})


class TTSVoiceIn(BaseModel):
    backend: str
    voice: str | None = None


@app.post("/api/audio/tts/voice")
async def api_tts_voice(body: TTSVoiceIn) -> JSONResponse:
    from .audio import tts as tts_mod

    try:
        info = tts_mod.set_voice(body.backend, body.voice)
        return JSONResponse({"ok": True, **info})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/audio/tts")
async def api_tts(body: TTSIn):
    from .audio import tts as tts_mod

    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    try:
        fp = await tts_mod.synthesize(text)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc

    media = "audio/mpeg" if str(fp).endswith(".mp3") else "audio/wav"
    return FileResponse(fp, media_type=media, filename=Path(fp).name)


class TTSPlayIn(BaseModel):
    text: str
    force: bool = False


@app.post("/api/audio/tts/play")
async def api_tts_play(body: TTSPlayIn) -> JSONResponse:
    """Synthesize and play through the system audio (macOS `say` style)."""
    from .audio import player
    from .audio import tts as tts_mod

    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    fp = await tts_mod.synthesize(text)
    await player.play_file(fp)
    return JSONResponse({"ok": True, "path": str(fp), "claim": True})


_AUDIO_FILE_RE = re.compile(r"^[\w.-]+\.(?:wav|mp3)$")


@app.get("/api/audio/file/{name}")
async def api_audio_file(name: str):
    from .audio import tts as tts_mod

    if not _AUDIO_FILE_RE.match(name):
        raise HTTPException(400, "bad file name")
    fp = tts_mod.tts_dir() / name
    if not fp.exists():
        raise HTTPException(404, "audio not found")
    media = "audio/mpeg" if name.endswith(".mp3") else "audio/wav"
    return FileResponse(fp, media_type=media)


@app.post("/api/audio/stt")
async def api_stt(audio: UploadFile = File(...)) -> JSONResponse:
    from .audio import stt as stt_mod

    data = await audio.read()
    try:
        text = await stt_mod.transcribe_bytes(data, suffix=Path(audio.filename or "audio.webm").suffix)
        return JSONResponse({"ok": True, "text": text})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(exc)}, status_code=500)


# ---- WebSocket -------------------------------------------------------------


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    queue = await BUS.subscribe()
    try:
        await ws.send_text(
            json.dumps(
                {
                    "t": time.time(),
                    "kind": "hello",
                    "payload": {
                        "version": "0.2.0",
                        "connections": await _connections_payload(),
                    },
                },
                ensure_ascii=False,
            )
        )
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=30)
                await ws.send_text(msg)
            except TimeoutError:
                await ws.send_text(json.dumps({"t": time.time(), "kind": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        BUS.unsubscribe(queue)


# ---- static UI -------------------------------------------------------------


_UI_DIR = _active_ui_dir()
if _UI_DIR is not None:
    app.mount("/static", StaticFiles(directory=str(_UI_DIR)), name="static")


@app.get("/")
async def root_index() -> FileResponse:
    if _UI_DIR is None:
        raise HTTPException(404, "UI not built — run `npm run build` in web-ui/ or use Vite dev server")
    return FileResponse(
        str(_UI_DIR / "index.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/favicon.ico")
async def favicon() -> FileResponse:
    if _UI_DIR is None:
        raise HTTPException(404)
    p = _UI_DIR / "favicon.ico"
    if p.exists():
        return FileResponse(str(p))
    raise HTTPException(404)
