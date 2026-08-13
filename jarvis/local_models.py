"""On-device LLM runner backed by Ollama.

The catalogue is AUTO-DISCOVERED from the Ollama daemon (`/api/tags`): every
model present on disk is offered in the UI, and `JARVIS_LOCAL_MODELS` in
`.env` only overrides per-model parameters (n_ctx, keep_alive, flags such as
`notools`). Models can be pulled from the UI; pulls run as background tasks
and report progress on the event bus.

Tool-calling capability is determined by a probe request on load (one short
chat call with a tool schema). Models that reject tools are flagged
`notools`, cached in `data/state.json`, and never receive tool schemas — so
they can never hallucinate tool calls into plain text.

Architecture: Ollama exposes an OpenAI-compatible endpoint at
`http://localhost:11434/v1/chat/completions`, so we lean on the same HTTP
streaming client used for the cloud providers. "Loaded" means the model is
resident in Ollama's memory (verified via `/api/ps` after a warmup request
with a long keep_alive). "Unload" sends a request with `keep_alive=0`.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from . import state_store
from .bus import BUS
from .config import SETTINGS

_OLLAMA_BASE = "http://localhost:11434"
_OLLAMA_OPENAI = f"{_OLLAMA_BASE}/v1"

_DISCOVER_TTL = 5.0
_CAPABILITIES_KEY = "local_model_capabilities"

_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "time_now",
        "description": "Vraća trenutno vreme.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def sanitize_history_for_notools(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip tool mechanics from history for models without function calling.

    `tool` messages are dropped; an `assistant` message carrying `tool_calls`
    keeps only its plain-text content (if any) and is dropped otherwise. The
    summary must NOT resemble a callable pattern — small models imitate
    whatever structure they see, so no tool names, no JSON, no "pozvan je
    alat" phrasing may leak into their context.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            continue
        if role == "assistant" and m.get("tool_calls"):
            content = (m.get("content") or "").strip()
            if content:
                out.append({"role": "assistant", "content": content})
            continue
        out.append(m)
    return out


class _ThinkTagStripper:
    """Streaming filter that removes inline `<think>...</think>` blocks.

    Safety net for reasoning models that ignore `think:false` and leak CoT
    into `content`. Holds back at most a few characters at chunk boundaries
    so an opening/closing tag split across deltas is still caught.
    """

    _OPEN_TAGS = ("<" + "think>", "<" + "think>\n")
    _CLOSE_TAG = "</" + "think>"

    def __init__(self) -> None:
        self._in_think = False
        self._buf = ""

    def _find_open(self) -> tuple[int, int]:
        best = (-1, 0)
        for tag in self._OPEN_TAGS:
            idx = self._buf.find(tag)
            if idx != -1 and (best[0] == -1 or idx < best[0]):
                best = (idx, len(tag))
        return best

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        out: list[str] = []
        while self._buf:
            if self._in_think:
                idx = self._buf.find(self._CLOSE_TAG)
                if idx == -1:
                    keep = len(self._CLOSE_TAG) - 1
                    if len(self._buf) > keep:
                        self._buf = self._buf[-keep:]
                    break
                self._buf = self._buf[idx + len(self._CLOSE_TAG) :]
                self._in_think = False
                continue
            idx, tag_len = self._find_open()
            close_idx = self._buf.find(self._CLOSE_TAG)
            if idx == -1 and close_idx == -1:
                keep = max(len(self._CLOSE_TAG), *(len(t) for t in self._OPEN_TAGS)) - 1
                if len(self._buf) > keep:
                    out.append(self._buf[:-keep])
                    self._buf = self._buf[-keep:]
                break
            if close_idx != -1 and (idx == -1 or close_idx < idx):
                out.append(self._buf[:close_idx])
                self._buf = self._buf[close_idx + len(self._CLOSE_TAG) :]
                continue
            out.append(self._buf[:idx])
            self._buf = self._buf[idx + tag_len :]
            self._in_think = True
        return "".join(out)

    def drain(self) -> str:
        if self._in_think:
            self._buf = ""
            return ""
        out = self._buf
        self._buf = ""
        return out


class LocalModelRunner:
    """Auto-discovered catalogue + Ollama bridge (load/unload/stream/pull)."""

    def __init__(self) -> None:
        self._state: str = "idle"
        self._loaded_id: str | None = None
        self._loaded_tag: str | None = None
        self._load_error: str | None = None
        self._loading_lock = asyncio.Lock()
        self._discover_cache: list[dict[str, Any]] | None = None
        self._discover_ts: float = 0.0
        self._capabilities: dict[str, str] | None = None
        self._pulls: dict[str, asyncio.Task] = {}
        self._pull_progress: dict[str, dict[str, Any]] = {}

    # ---- engine reachability ------------------------------------------------

    def available(self) -> bool:
        """Sync probe — only for contexts without an event loop."""
        try:
            with httpx.Client(timeout=1.5) as client:
                r = client.get(f"{_OLLAMA_BASE}/api/version")
                return r.status_code == 200
        except Exception:
            return False

    async def available_async(self) -> bool:
        def _do() -> bool:
            try:
                with httpx.Client(timeout=1.5) as client:
                    r = client.get(f"{_OLLAMA_BASE}/api/version")
                    return r.status_code == 200
            except Exception:
                return False

        return await asyncio.to_thread(_do)

    # ---- catalogue (auto-discovery) -----------------------------------------

    def _env_entry_for_tag(self, tag: str) -> dict[str, Any] | None:
        for e in SETTINGS.local_models.entries:
            if e["tag"].lower() == tag.lower():
                return e
        return None

    async def _capabilities_map(self) -> dict[str, str]:
        """tag -> "tools" | "notools". Explicit .env flags win over probes."""
        if self._capabilities is None:
            cached = await state_store.get_state_value(_CAPABILITIES_KEY, {})
            self._capabilities = cached if isinstance(cached, dict) else {}
        merged: dict[str, str] = dict(self._capabilities)
        for e in SETTINGS.local_models.entries:
            flags = {f.strip() for f in (e.get("flags") or "").split(",") if f.strip()}
            if "notools" in flags:
                merged[e["tag"].lower()] = "notools"
            elif "tools" in flags:
                merged[e["tag"].lower()] = "tools"
        return merged

    async def discover(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Every model Ollama has on disk, merged with .env overrides.

        Cached for a few seconds so UI polls and the model dropdown do not
        hammer the daemon. Each entry:
        ``{id, label, tag, n_ctx, keep_alive, size, modified_at, in_ram,
           ready, capability}`` where capability is ``"tools" | "notools" |
           None`` (None = not probed yet).
        """
        now = time.monotonic()
        if not force and self._discover_cache is not None and now - self._discover_ts < _DISCOVER_TTL:
            return self._discover_cache

        if not await self.available_async():
            self._discover_cache = []
            self._discover_ts = now
            return []

        tags_data, ps_tags = await asyncio.gather(self._api_tags(), self._ps_tags())
        caps = await self._capabilities_map()

        out: list[dict[str, Any]] = []
        seen_env_ids: set[str] = set()
        for m in tags_data:
            tag = m.get("name") or ""
            if not tag:
                continue
            env = self._env_entry_for_tag(tag)
            if env is not None:
                mid, label = env["id"], env["label"]
                n_ctx, keep_alive = env["n_ctx"], env["keep_alive"]
                seen_env_ids.add(mid)
            else:
                mid, label, n_ctx, keep_alive = tag, tag, 32768, "24h"
            out.append(
                {
                    "id": mid,
                    "label": label,
                    "tag": tag,
                    "n_ctx": n_ctx,
                    "keep_alive": keep_alive,
                    "size": m.get("size") or 0,
                    "modified_at": m.get("modified_at") or "",
                    "in_ram": tag.lower() in {t.lower() for t in ps_tags},
                    "ready": self.is_ready(mid),
                    "capability": caps.get(tag.lower()),
                }
            )
        for e in SETTINGS.local_models.entries:
            if e["id"] in seen_env_ids:
                continue
            out.append(
                {
                    "id": e["id"],
                    "label": e["label"],
                    "tag": e["tag"],
                    "n_ctx": e["n_ctx"],
                    "keep_alive": e["keep_alive"],
                    "size": 0,
                    "modified_at": "",
                    "in_ram": False,
                    "ready": self.is_ready(e["id"]),
                    "capability": caps.get(e["tag"].lower()),
                }
            )
        self._discover_cache = out
        self._discover_ts = now
        return out

    def invalidate_discovery(self) -> None:
        self._discover_cache = None
        self._discover_ts = 0.0

    async def _api_tags(self) -> list[dict[str, Any]]:
        def _do() -> list[dict[str, Any]]:
            try:
                with httpx.Client(timeout=5) as client:
                    r = client.get(f"{_OLLAMA_BASE}/api/tags")
                    if r.status_code != 200:
                        return []
                    return r.json().get("models") or []
            except Exception:
                return []

        return await asyncio.to_thread(_do)

    # ---- status --------------------------------------------------------------

    async def astatus(self) -> dict[str, Any]:
        return {
            "engine_available": await self.available_async(),
            "state": self._state,
            "loaded_id": self._loaded_id,
            "loaded_tag": self._loaded_tag,
            "error": self._load_error,
        }

    def status(self) -> dict[str, Any]:
        return {
            "engine_available": self.available(),
            "state": self._state,
            "loaded_id": self._loaded_id,
            "loaded_tag": self._loaded_tag,
            "error": self._load_error,
        }

    def is_ready(self, model_id: str) -> bool:
        return self._state == "ready" and self._loaded_id == model_id

    def capability_for(self, tag: str) -> str | None:
        if self._capabilities is None:
            return None
        return self._capabilities.get(tag.lower())

    async def capability_for_model(self, model_id: str) -> str | None:
        """Capability ("tools" | "notools" | None) for a catalogue model id,
        resolving .env flags and the persisted probe cache."""
        entry = await self._resolve_entry(model_id)
        if entry is None:
            return None
        caps = await self._capabilities_map()
        return caps.get(entry["tag"].lower())

    # ---- pulls (background, never block the server) --------------------------

    def pulls_status(self) -> list[dict[str, Any]]:
        return [dict(p) for p in self._pull_progress.values()]

    def is_pulling(self, tag: str) -> bool:
        t = self._pulls.get(tag)
        return t is not None and not t.done()

    async def start_pull(self, tag: str) -> dict[str, Any]:
        tag = tag.strip()
        if not tag:
            raise RuntimeError("empty tag")
        if self.is_pulling(tag):
            return {"ok": True, "already_pulling": True, "tag": tag}
        if not await self.available_async():
            raise RuntimeError("Ollama daemon nije pokrenut (localhost:11434)")
        task = asyncio.create_task(self._pull_worker(tag))
        self._pulls[tag] = task
        return {"ok": True, "tag": tag}

    async def cancel_pull(self, tag: str) -> dict[str, Any]:
        task = self._pulls.get(tag)
        if task is None or task.done():
            return {"ok": False, "error": "no active pull for that tag"}
        task.cancel()
        return {"ok": True, "tag": tag}

    async def _pull_worker(self, tag: str) -> None:
        progress = {"tag": tag, "status": "starting", "percent": 0.0, "detail": ""}
        self._pull_progress[tag] = progress
        await BUS.publish("local_model_pulling", dict(progress))
        url = f"{_OLLAMA_BASE}/api/pull"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None)) as client:
                async with client.stream("POST", url, json={"name": tag, "stream": True}) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        raise RuntimeError(
                            f"Ollama HTTP {resp.status_code}: {body.decode(errors='replace')[:300]}"
                        )
                    async for raw in resp.aiter_lines():
                        if not raw.strip():
                            continue
                        try:
                            evt = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if "error" in evt:
                            raise RuntimeError(str(evt["error"]))
                        status = evt.get("status") or ""
                        completed = evt.get("completed") or 0
                        total = evt.get("total") or 0
                        if total > 0:
                            progress.update(
                                {
                                    "status": "progress",
                                    "percent": round(100.0 * completed / total, 1),
                                    "detail": status,
                                }
                            )
                        else:
                            progress.update({"detail": status})
                        await BUS.publish("local_model_pulling", dict(progress))
            progress.update({"status": "done", "percent": 100.0})
            await BUS.publish("local_model_pulling", dict(progress))
            self.invalidate_discovery()
        except asyncio.CancelledError:
            progress.update({"status": "cancelled"})
            await BUS.publish("local_model_pulling", dict(progress))
            raise
        except Exception as exc:  # noqa: BLE001
            progress.update({"status": "error", "detail": str(exc)[:300]})
            await BUS.publish("local_model_pulling", dict(progress))
        finally:
            self._pulls.pop(tag, None)
            asyncio.get_running_loop().call_later(30.0, lambda t=tag: self._pull_progress.pop(t, None))

    # ---- capability probe ------------------------------------------------------

    async def probe_tools(self, tag: str) -> bool | None:
        """One short request with a tool schema. True = supports function
        calling, False = rejects tools (HTTP 400), None = inconclusive."""

        def _do() -> bool | None:
            body = {
                "model": tag,
                "messages": [{"role": "user", "content": "Koliko je 2+2?"}],
                "tools": [_PROBE_TOOL],
                "stream": False,
                "think": False,
            }
            try:
                with httpx.Client(timeout=120) as client:
                    r = client.post(f"{_OLLAMA_BASE}/api/chat", json=body)
            except Exception:
                return None
            if r.status_code == 400:
                text = r.text.lower()
                if "does not support tools" in text or "function calling" in text:
                    return False
                return None
            if r.status_code == 200:
                return True
            return None

        result = await asyncio.to_thread(_do)
        if result is not None:
            if self._capabilities is None:
                cached = await state_store.get_state_value(_CAPABILITIES_KEY, {})
                self._capabilities = cached if isinstance(cached, dict) else {}
            self._capabilities[tag.lower()] = "tools" if result else "notools"
            await state_store.set_state_value(_CAPABILITIES_KEY, self._capabilities)
        return result

    # ---- lifecycle --------------------------------------------------------------

    async def load(self, model_id: str) -> dict[str, Any]:
        """Load a model into Ollama's RAM (warmup with long keep_alive),
        verify via `/api/ps`, then probe tool capability if unknown."""
        if not await self.available_async():
            raise RuntimeError(
                "Ollama daemon nije dostupan. Pokreni `ollama serve` (ili `brew services start ollama`)."
            )
        entry = await self._resolve_entry(model_id)
        if entry is None:
            raise RuntimeError(f"nepoznat lokalni model: {model_id!r}")
        tag = entry["tag"]

        async with self._loading_lock:
            if self._state == "ready" and self._loaded_id == model_id:
                return await self.astatus()
            if self._state == "loading":
                raise RuntimeError("another local model load is already in progress")

            self._state = "loading"
            self._load_error = None
            self._loaded_id = model_id
            self._loaded_tag = tag
            await BUS.publish("local_model_loading", {"id": model_id, "tag": tag})
            try:
                if not await self._has_model(tag):
                    raise RuntimeError(
                        f"model '{tag}' nije skinut. Povuci ga dugmetom Pull u UI "
                        f"ili sa `ollama pull {tag}` pa pokušaj opet."
                    )
                await self._warmup(tag, keep_alive=entry.get("keep_alive") or "24h")
                loaded_tags = await self._ps_tags()
                if tag.lower() not in {t.lower() for t in loaded_tags}:
                    raise RuntimeError(f"Ollama nije učitao {tag!r} posle warmup-a (učitano: {loaded_tags})")
                if self.capability_for(tag) is None:
                    await self.probe_tools(tag)
            except Exception as exc:  # noqa: BLE001
                self._state = "error"
                self._load_error = str(exc)
                self._loaded_id = None
                self._loaded_tag = None
                payload = await self.astatus()
                await BUS.publish("local_model_error", payload)
                raise RuntimeError(f"failed to load {tag}: {exc}") from exc

            self._state = "ready"
            self.invalidate_discovery()
            payload = await self.astatus()
            payload["capability"] = self.capability_for(tag)
            await BUS.publish("local_model_ready", payload)
            return payload

    async def _resolve_entry(self, model_id: str) -> dict[str, Any] | None:
        for e in SETTINGS.local_models.entries:
            if e["id"] == model_id:
                return e
        if ":" in model_id:
            return {
                "id": model_id,
                "label": model_id,
                "tag": model_id,
                "n_ctx": 32768,
                "keep_alive": "24h",
                "flags": "",
            }
        return None

    async def _has_model(self, tag: str) -> bool:
        tags = [m.get("name", "") for m in await self._api_tags() if m.get("name")]
        return tag.lower() in {t.lower() for t in tags}

    async def unload(self) -> dict[str, Any]:
        """Tell Ollama to drop the loaded model immediately (`keep_alive=0`)."""
        async with self._loading_lock:
            if self._loaded_tag is not None:
                try:
                    await self._unload_tag(self._loaded_tag)
                except Exception:  # noqa: BLE001
                    pass
            self._state = "idle"
            self._loaded_id = None
            self._loaded_tag = None
            self._load_error = None
            self.invalidate_discovery()
            payload = await self.astatus()
            await BUS.publish("local_model_unloaded", payload)
            return payload

    async def _warmup(self, tag: str, *, keep_alive: str) -> None:
        body = {"model": tag, "prompt": "hi", "stream": False, "keep_alive": keep_alive}
        await asyncio.to_thread(lambda: self._post_json(f"{_OLLAMA_BASE}/api/generate", body, timeout=300))

    async def _unload_tag(self, tag: str) -> None:
        body = {"model": tag, "prompt": "", "keep_alive": 0}
        await asyncio.to_thread(lambda: self._post_json(f"{_OLLAMA_BASE}/api/generate", body, timeout=10))

    async def _ps_tags(self) -> list[str]:
        def _do() -> list[str]:
            try:
                with httpx.Client(timeout=5) as client:
                    r = client.get(f"{_OLLAMA_BASE}/api/ps")
                    if r.status_code != 200:
                        return []
                    return [m.get("name", "") for m in (r.json().get("models") or []) if m.get("name")]
            except Exception:
                return []

        return await asyncio.to_thread(_do)

    def _post_json(self, url: str, body: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=body)
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            try:
                return r.json()
            except Exception:
                return {}

    # ---- chat ---------------------------------------------------------------------

    async def stream_chat(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        timeout: float = 600.0,
    ) -> AsyncIterator[tuple[str, Any]]:
        """Stream chat completions via Ollama's OpenAI-compat endpoint.

        Models flagged `notools` never receive tool schemas and get a
        sanitized history (no `tool_calls` / `tool` messages), so they cannot
        hallucinate tool calls as text. The HTTP-400 retry without tools is
        kept as a safety net for unprobed models. `think:false` disables CoT
        for reasoning models (Qwen3); a streaming stripper catches any
        ``<think>`` blocks that still leak into content.
        """
        if not self.is_ready(model_id):
            raise RuntimeError(f"lokalni model {model_id!r} nije učitan u RAM")

        entry = await self._resolve_entry(model_id)
        if entry is None:
            raise RuntimeError(f"lokalni model {model_id!r} više ne postoji u katalogu")
        tag = entry["tag"]
        keep_alive = entry.get("keep_alive") or "24h"

        capability = self.capability_for(tag)
        if capability == "notools":
            tools = None
            messages = sanitize_history_for_notools(messages)

        body: dict[str, Any] = {
            "model": tag,
            "messages": messages,
            "stream": True,
            "keep_alive": keep_alive,
            "think": False,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if temperature is not None:
            body["temperature"] = float(temperature)

        url = f"{_OLLAMA_OPENAI}/chat/completions"
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}

        finish_reason: str | None = None
        text_buf: str = ""
        reasoning_buf: str = ""
        tool_calls_out: list[dict[str, Any]] = []
        stripper = _ThinkTagStripper()

        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                use_tools = bool(tools)
                while True:
                    body_now = dict(body)
                    if not use_tools:
                        body_now.pop("tools", None)
                        body_now.pop("tool_choice", None)
                    async with client.stream("POST", url, json=body_now, headers=headers) as resp:
                        if resp.status_code == 400 and use_tools:
                            peek = await resp.aread()
                            decoded = peek.decode(errors="replace")
                            if "does not support tools" in decoded or "function calling" in decoded.lower():
                                use_tools = False
                                if self._capabilities is not None:
                                    self._capabilities[tag.lower()] = "notools"
                                    await state_store.set_state_value(_CAPABILITIES_KEY, self._capabilities)
                                continue
                            raise RuntimeError(f"Ollama HTTP 400: {decoded[:500]}")
                        if resp.status_code >= 400:
                            text = await resp.aread()
                            raise RuntimeError(
                                f"Ollama HTTP {resp.status_code}: {text.decode(errors='replace')[:500]}"
                            )
                        async for raw in resp.aiter_lines():
                            if not raw:
                                continue
                            if raw.startswith(":"):
                                continue
                            payload = raw[5:].strip() if raw.startswith("data:") else raw.strip()
                            if not payload or payload == "[DONE]":
                                continue
                            try:
                                evt = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            for choice in evt.get("choices") or []:
                                delta = choice.get("delta") or {}
                                if choice.get("finish_reason"):
                                    finish_reason = str(choice["finish_reason"])
                                    yield "finish", finish_reason
                                content = delta.get("content") or ""
                                reasoning = delta.get("reasoning") or ""
                                if content:
                                    clean = stripper.feed(content)
                                    if clean:
                                        text_buf += clean
                                        yield "delta", clean
                                if reasoning:
                                    reasoning_buf += reasoning
                                    yield "reasoning", reasoning
                                for tc in delta.get("tool_calls") or []:
                                    fn = tc.get("function") or {}
                                    args = fn.get("arguments") or ""
                                    if isinstance(args, dict):
                                        args = json.dumps(args, ensure_ascii=False)
                                    tool_calls_out.append(
                                        {
                                            "id": tc.get("id") or f"call_local_{len(tool_calls_out)}",
                                            "type": "function",
                                            "function": {
                                                "name": fn.get("name") or "",
                                                "arguments": args,
                                            },
                                        }
                                    )
                            if "error" in evt and not finish_reason:
                                raise RuntimeError(f"Ollama stream error: {evt.get('error')}")
                        break
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Ollama network error: {exc}") from exc

        tail = stripper.drain()
        if tail:
            text_buf += tail
            yield "delta", tail

        done = {
            "role": "assistant",
            "content": text_buf,
            "tool_calls": tool_calls_out,
            "finish_reason": finish_reason,
        }
        yield "done", done


RUNNER = LocalModelRunner()
