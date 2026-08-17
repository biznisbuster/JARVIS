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
import re
import time
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx

from . import state_store
from .bus import BUS
from .config import SETTINGS
from .tool_calls import ToolCallAccumulator

_OLLAMA_BASE = "http://localhost:11434"
_OLLAMA_OPENAI = f"{_OLLAMA_BASE}/v1"

_DISCOVER_TTL = 5.0
_UNLOAD_VERIFY_TIMEOUT = 5.0
_UNLOAD_VERIFY_INTERVAL = 0.1
_CAPABILITIES_KEY = "local_model_capabilities"
Capability = Literal["tools", "notools", "unknown"]
RunnerState = Literal["idle", "loading", "ready", "error", "unloading"]
_CAPABILITIES = frozenset({"tools", "notools", "unknown"})

_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "time_now",
        "description": "Vraća trenutno vreme.",
        "parameters": {"type": "object", "properties": {}},
    },
}


_TOOL_MECHANICS_RE = re.compile(
    r"(?:\b(?:tool|function)[ _-]?call\b|[\"'](?:id|name|arguments|tool_calls)[\"']\s*:"
    r"|\b(?:poziv|pozva\w*|pozovi\w*)\s+(?:alat|funkc\w*)"
    r"|\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b)",
    re.IGNORECASE,
)


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
            if content and not _TOOL_MECHANICS_RE.search(content):
                out.append({"role": "assistant", "content": content})
            continue
        # Return a derived request-time history.  The canonical session
        # history must retain its valid OpenAI tool-call groups for a later
        # cloud-model turn.
        out.append(dict(m))
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


class LocalModelError(RuntimeError):
    """Base error for a local-model lifecycle or readiness failure."""

    code = "EXECUTION_FAILED"


class LocalModelNotReadyError(LocalModelError):
    """The requested local model was not confirmed ready before generation."""

    code = "NOT_READY"


class LocalModelBusyError(LocalModelError):
    """A lifecycle operation would interfere with an active local stream."""

    code = "NOT_AVAILABLE"


class LocalModelRunner:
    """Auto-discovered Ollama bridge with an explicit runner state machine.

    ``loaded_id``/``loaded_tag`` are only populated after warmup and an Ollama
    ``/api/ps`` check positively confirm residency. ``target_id``/
    ``target_tag`` identify the model involved in a loading or unloading
    transition and are never used as readiness evidence. Lifecycle operations
    are serialized in request order by ``_loading_lock``; a later load waits
    for an in-flight load and then performs its own verified transition. Local
    generation increments ``active_streams`` before opening the provider
    stream and decrements it in ``finally`` so unload cannot interrupt it.
    """

    def __init__(self) -> None:
        self._state: RunnerState = "idle"
        self._loaded_id: str | None = None
        self._loaded_tag: str | None = None
        self._target_id: str | None = None
        self._target_tag: str | None = None
        self._load_error: str | None = None
        self._active_streams = 0
        self._loading_lock = asyncio.Lock()
        self._discover_cache: list[dict[str, Any]] | None = None
        self._discover_ts: float = 0.0
        self._capabilities: dict[str, dict[str, Any]] | None = None
        self._tag_identities: dict[str, dict[str, Any]] = {}
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

    def _explicit_capability(self, tag: str) -> Capability | None:
        """Return a configured override, if one exists for ``tag``."""

        for entry in SETTINGS.local_models.entries:
            if entry["tag"].lower() != tag.lower():
                continue
            flags = {f.strip().lower() for f in (entry.get("flags") or "").split(",") if f.strip()}
            if "notools" in flags:
                return "notools"
            if "tools" in flags:
                return "tools"
        return None

    @staticmethod
    def _identity_from_tag_record(record: dict[str, Any]) -> dict[str, Any]:
        """Keep only stable model identity fields exposed by Ollama."""

        identity: dict[str, Any] = {}
        for key in ("digest", "modified_at", "size"):
            value = record.get(key)
            if value is not None and value != "":
                identity[key] = value
        return identity

    def _remember_tag_identities(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            tag = record.get("name") or record.get("model")
            if isinstance(tag, str) and tag:
                self._tag_identities[tag.lower()] = self._identity_from_tag_record(record)

    @staticmethod
    def _normalize_capability_cache(raw: Any) -> dict[str, dict[str, Any]]:
        """Normalize both the Phase 0 string cache and the identity-aware form."""

        if not isinstance(raw, dict):
            return {}
        normalized: dict[str, dict[str, Any]] = {}
        for raw_tag, raw_entry in raw.items():
            if not isinstance(raw_tag, str):
                continue
            if isinstance(raw_entry, str) and raw_entry in _CAPABILITIES:
                # Legacy entries have no identity and must be re-probed when
                # a current Ollama identity is available.
                normalized[raw_tag.lower()] = {"capability": raw_entry, "identity": None}
                continue
            if not isinstance(raw_entry, dict):
                continue
            capability = raw_entry.get("capability")
            if capability not in _CAPABILITIES:
                continue
            identity = raw_entry.get("identity")
            normalized[raw_tag.lower()] = {
                "capability": capability,
                "identity": dict(identity) if isinstance(identity, dict) else None,
            }
        return normalized

    async def _load_capability_cache(self) -> None:
        if self._capabilities is None:
            cached = await state_store.get_state_value(_CAPABILITIES_KEY, {})
            self._capabilities = self._normalize_capability_cache(cached)

    def _cache_entry_is_valid(self, tag: str, entry: dict[str, Any] | None) -> bool:
        if entry is None or entry.get("identity") is None:
            return False
        current = self._tag_identities.get(tag.lower())
        stored = entry.get("identity")
        # A direct probe can legitimately run when /api/tags does not expose
        # identity data.  Such an empty identity remains usable until Ollama
        # later supplies real metadata, at which point it is invalidated.
        if current is None:
            return stored == {}
        return stored == current

    def _effective_capability(self, tag: str) -> Capability:
        explicit = self._explicit_capability(tag)
        if explicit is not None:
            return explicit
        if self._capabilities is None:
            return "unknown"
        raw_entry = self._capabilities.get(tag.lower())
        if isinstance(raw_entry, str):  # defensive compatibility for tests/callers
            return raw_entry if raw_entry in _CAPABILITIES else "unknown"
        if not isinstance(raw_entry, dict) or not self._cache_entry_is_valid(tag, raw_entry):
            return "unknown"
        capability = raw_entry.get("capability")
        return capability if capability in _CAPABILITIES else "unknown"

    async def _capabilities_map(self) -> dict[str, Capability]:
        """Return effective capabilities; explicit .env flags always win."""

        await self._load_capability_cache()
        merged: dict[str, Capability] = {
            tag: self._effective_capability(tag) for tag in (self._capabilities or {})
        }
        for entry in SETTINGS.local_models.entries:
            merged[entry["tag"].lower()] = self._effective_capability(entry["tag"])
        return merged

    async def _capability_cache_valid(self, tag: str) -> bool:
        await self._load_capability_cache()
        entry = (self._capabilities or {}).get(tag.lower())
        return bool(
            isinstance(entry, dict)
            and entry.get("capability") in {"tools", "notools"}
            and self._cache_entry_is_valid(tag, entry)
        )

    async def _persist_capability(self, tag: str, capability: Capability) -> None:
        await self._load_capability_cache()
        identity = dict(self._tag_identities.get(tag.lower(), {}))
        self._capabilities[tag.lower()] = {"capability": capability, "identity": identity}
        await state_store.set_state_value(_CAPABILITIES_KEY, self._capabilities)
        self.invalidate_discovery()

    async def discover(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Every model Ollama has on disk, merged with .env overrides.

        Cached for a few seconds so UI polls and the model dropdown do not
        hammer the daemon. Each entry:
        ``{id, label, tag, n_ctx, keep_alive, size, modified_at, in_ram,
           ready, capability}`` where capability is ``"tools" | "notools" |
           "unknown"``.
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
                    "capability": caps.get(tag.lower(), "unknown"),
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
                    "capability": caps.get(e["tag"].lower(), "unknown"),
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

        records = await asyncio.to_thread(_do)
        self._remember_tag_identities(records)
        return records

    # ---- status --------------------------------------------------------------

    def _status_snapshot(self, *, engine_available: bool) -> dict[str, Any]:
        return {
            "engine_available": engine_available,
            "state": self._state,
            "loaded_id": self._loaded_id,
            "loaded_tag": self._loaded_tag,
            "target_id": self._target_id,
            "target_tag": self._target_tag,
            "error": self._load_error,
            "active_streams": self._active_streams,
        }

    async def astatus(self) -> dict[str, Any]:
        return self._status_snapshot(engine_available=await self.available_async())

    def status(self) -> dict[str, Any]:
        return self._status_snapshot(engine_available=self.available())

    def is_ready(self, model_id: str) -> bool:
        return self._state == "ready" and self._loaded_id == model_id

    def capability_for(self, tag: str) -> Capability:
        """Return ``tools``, ``notools`` or ``unknown`` for a model tag."""

        return self._effective_capability(tag)

    async def capability_for_model(self, model_id: str) -> Capability:
        """Resolve a catalogue model's identity-aware capability."""

        entry = await self._resolve_entry(model_id)
        if entry is None:
            return "unknown"
        if self._explicit_capability(entry["tag"]) is not None:
            return self._explicit_capability(entry["tag"]) or "unknown"
        # Refresh the identity before trusting a persisted capability.  The
        # caller is already asking about a local model, so this bounded tags
        # request is the point where changed digests/modified_at are noticed.
        await self._api_tags()
        await self._capabilities_map()
        return self.capability_for(entry["tag"])

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

    @staticmethod
    def _probe_tool_calls(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        calls: list[dict[str, Any]] = []

        def add(value: Any) -> None:
            if isinstance(value, list):
                calls.extend(call for call in value if isinstance(call, dict))

        add(payload.get("tool_calls"))
        message = payload.get("message")
        if isinstance(message, dict):
            add(message.get("tool_calls"))
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            add(choice.get("tool_calls"))
            choice_message = choice.get("message") or choice.get("delta")
            if isinstance(choice_message, dict):
                add(choice_message.get("tool_calls"))
        return calls

    @classmethod
    def _probe_has_valid_tool_call(cls, payload: Any) -> bool:
        for call in cls._probe_tool_calls(payload):
            function = call.get("function") or {}
            if not isinstance(function, dict) or function.get("name") != "time_now":
                continue
            arguments = function.get("arguments")
            if arguments in (None, "") or isinstance(arguments, dict):
                return True
            if isinstance(arguments, str):
                try:
                    parsed = json.loads(arguments) if arguments.strip() else {}
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return True
        return False

    @staticmethod
    def _probe_has_plain_text(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        if isinstance(payload.get("response"), str) and payload["response"].strip():
            return True
        message = payload.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return bool(message["content"].strip())
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            for candidate in (choice.get("message"), choice.get("delta"), choice):
                if isinstance(candidate, dict) and isinstance(candidate.get("content"), str):
                    if candidate["content"].strip():
                        return True
        return False

    @classmethod
    def _classify_probe_response(cls, response: Any) -> Capability:
        if response.status_code == 400:
            text = str(getattr(response, "text", "")).lower()
            if "does not support tools" in text or "function calling" in text:
                return "notools"
            return "unknown"
        if response.status_code != 200:
            return "unknown"
        try:
            payload = response.json()
        except (AttributeError, TypeError, ValueError):
            return "unknown"
        if cls._probe_has_valid_tool_call(payload):
            return "tools"
        # A well-formed ordinary answer to a probe that explicitly required a
        # call is positive evidence of a no-tools model, not tool support.
        if cls._probe_has_plain_text(payload):
            return "notools"
        return "unknown"

    @staticmethod
    def _has_valid_runtime_tool_call(tool_calls: list[dict[str, Any]]) -> bool:
        """Return whether runtime output is positive native-tool evidence.

        The finalized public calls deliberately retain malformed arguments so
        the agent execution boundary can reject them.  Capability promotion
        needs the stricter semantic check: a named call whose arguments are
        valid JSON representing an object.
        """

        for call in tool_calls:
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if not isinstance(name, str) or not name.strip():
                continue

            arguments = function.get("arguments")
            if isinstance(arguments, dict):
                parsed = arguments
            elif isinstance(arguments, str):
                try:
                    parsed = json.loads(arguments)
                except (TypeError, ValueError):
                    continue
            else:
                continue
            if isinstance(parsed, dict):
                return True
        return False

    async def probe_tools(self, tag: str) -> Capability:
        """Probe native tool calling and persist ``tools/notools/unknown``."""

        explicit = self._explicit_capability(tag)
        if explicit is not None:
            return explicit

        def _do() -> Capability:
            body = {
                "model": tag,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "You must answer by calling the provided time_now function. "
                            "Do not answer in plain text."
                        ),
                    }
                ],
                "tools": [_PROBE_TOOL],
                "stream": False,
                "think": False,
                "options": {"temperature": 0},
            }
            try:
                with httpx.Client(timeout=120) as client:
                    r = client.post(f"{_OLLAMA_BASE}/api/chat", json=body)
            except Exception:
                return "unknown"
            return self._classify_probe_response(r)

        result = await asyncio.to_thread(_do)
        await self._persist_capability(tag, result)
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
                self._load_error = None
                return await self.astatus()
            if self._active_streams:
                raise LocalModelBusyError("ne mogu da promenim lokalni model dok generacija traje")

            previous_id = self._loaded_id if self._state == "ready" else None
            previous_tag = self._loaded_tag if self._state == "ready" else None

            self._state = "loading"
            self._load_error = None
            self._target_id = model_id
            self._target_tag = tag
            await self._publish_lifecycle("local_model_loading", id=model_id, tag=tag)
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
                if self._explicit_capability(tag) is None and not await self._capability_cache_valid(tag):
                    await self.probe_tools(tag)
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
                self._target_id = None
                self._target_tag = None
                if previous_id is not None and previous_tag is not None:
                    previous_tags = await self._ps_tags()
                    if previous_tag.lower() in {item.lower() for item in previous_tags}:
                        self._state = "ready"
                        self._loaded_id = previous_id
                        self._loaded_tag = previous_tag
                        self._load_error = error
                        await self._publish_lifecycle(
                            "local_model_error",
                            id=model_id,
                            tag=tag,
                            failed_id=model_id,
                            failed_tag=tag,
                        )
                        raise RuntimeError(f"failed to load {tag}: {exc}") from exc
                self._state = "error"
                self._load_error = error
                self._loaded_id = None
                self._loaded_tag = None
                await self._publish_lifecycle(
                    "local_model_error",
                    id=model_id,
                    tag=tag,
                    failed_id=model_id,
                    failed_tag=tag,
                )
                raise RuntimeError(f"failed to load {tag}: {exc}") from exc

            self._state = "ready"
            self._loaded_id = model_id
            self._loaded_tag = tag
            self._target_id = None
            self._target_tag = None
            self._load_error = None
            self.invalidate_discovery()
            return await self._publish_lifecycle(
                "local_model_ready",
                id=model_id,
                tag=tag,
                capability=self.capability_for(tag),
            )

    async def _publish_lifecycle(
        self,
        kind: str,
        *,
        id: str | None = None,
        tag: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        payload = await self.astatus()
        if id is not None:
            payload["id"] = id
        if tag is not None:
            payload["tag"] = tag
        payload.update(extra)
        await BUS.publish(kind, payload)
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
            if self._active_streams:
                raise LocalModelBusyError("ne mogu da oslobodim lokalni model dok generacija traje")

            model_id = self._loaded_id
            tag = self._loaded_tag
            if model_id is None or tag is None:
                self._state = "idle"
                self._target_id = None
                self._target_tag = None
                self._load_error = None
                self.invalidate_discovery()
                return await self._publish_lifecycle("local_model_unloaded")

            self._state = "unloading"
            self._target_id = model_id
            self._target_tag = tag
            await self._publish_lifecycle("local_model_unloading", id=model_id, tag=tag)
            try:
                await self._unload_tag(tag)
                if not await self._wait_until_unloaded(tag):
                    raise RuntimeError(f"Ollama i dalje drži {tag!r} u RAM-u posle unload-a")
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
                self._target_id = None
                self._target_tag = None
                resident_tags = await self._ps_tags()
                if tag.lower() in {item.lower() for item in resident_tags}:
                    self._state = "ready"
                    self._loaded_id = model_id
                    self._loaded_tag = tag
                else:
                    self._state = "error"
                    self._loaded_id = None
                    self._loaded_tag = None
                self._load_error = error
                await self._publish_lifecycle(
                    "local_model_error",
                    id=model_id,
                    tag=tag,
                    failed_id=model_id,
                    failed_tag=tag,
                )
                raise RuntimeError(f"failed to unload {tag}: {exc}") from exc

            self._state = "idle"
            self._loaded_id = None
            self._loaded_tag = None
            self._target_id = None
            self._target_tag = None
            self._load_error = None
            self.invalidate_discovery()
            return await self._publish_lifecycle("local_model_unloaded", id=model_id, tag=tag)

    async def _wait_until_unloaded(self, tag: str) -> bool:
        """Wait briefly for Ollama's asynchronous keep-alive eviction."""

        deadline = time.monotonic() + _UNLOAD_VERIFY_TIMEOUT
        while True:
            loaded_tags = await self._ps_tags()
            if tag.lower() not in {item.lower() for item in loaded_tags}:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(_UNLOAD_VERIFY_INTERVAL, remaining))

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
        async with self._loading_lock:
            if not self.is_ready(model_id):
                raise LocalModelNotReadyError(f"lokalni model {model_id!r} nije spreman za chat")
            self._active_streams += 1

        try:
            entry = await self._resolve_entry(model_id)
            if entry is None:
                raise LocalModelNotReadyError(f"lokalni model {model_id!r} više ne postoji u katalogu")
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
            tool_accumulator = ToolCallAccumulator()
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
                                decoded_lower = decoded.lower()
                                if (
                                    "does not support tools" in decoded_lower
                                    or "function calling" in decoded_lower
                                ):
                                    use_tools = False
                                    await self._persist_capability(tag, "notools")
                                    messages = sanitize_history_for_notools(messages)
                                    body["messages"] = messages
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
                                    for container in (delta, choice.get("message") or {}):
                                        for tc in container.get("tool_calls") or []:
                                            tool_accumulator.absorb(tc)
                                if "error" in evt and not finish_reason:
                                    raise RuntimeError(f"Ollama stream error: {evt.get('error')}")
                            break
                except httpx.HTTPError as exc:
                    raise RuntimeError(f"Ollama network error: {exc}") from exc

            tail = stripper.drain()
            if tail:
                text_buf += tail
                yield "delta", tail

            tool_calls_out = tool_accumulator.finalize()
            if use_tools and self._has_valid_runtime_tool_call(tool_calls_out):
                # Keep the probe/cache identity consistent with the model
                # currently loaded in Ollama.  Malformed or non-object arguments
                # remain in the public calls for execution-boundary rejection but
                # are not positive capability evidence.
                await self._persist_capability(tag, "tools")

            done = {
                "role": "assistant",
                "content": text_buf,
                "tool_calls": tool_calls_out,
                "finish_reason": finish_reason,
            }
            yield "done", done
        finally:
            self._active_streams -= 1


RUNNER = LocalModelRunner()
