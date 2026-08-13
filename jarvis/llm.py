"""OpenAI-compatible streaming chat client.

Targets the MiniMax token-plan endpoint (or Bailian/QwenCloud) configured
via `.env` / `~/.config/kilo/kilo.jsonc`. Supports streaming text deltas,
native `reasoning_content` (thinking) deltas and incremental tool-call
assembly, which is what the agent loop needs to decide between continuing
the answer or handing control to a tool.

A single `httpx.AsyncClient` with keep-alive is shared by all turns so we
pay the TLS handshake once, not on every LLM call.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from .bus import BUS
from .config import SETTINGS

_MAX_RETRIES = 2
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), 5.0)
        except ValueError:
            pass
    return 0.7 * (2.5**attempt) + random.uniform(0.0, 0.3)


class LLMError(RuntimeError):
    pass


@dataclass
class AssistantMessage:
    role: str = "assistant"
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None

    def to_openai(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = self.tool_calls
        return out


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=None,
            limits=httpx.Limits(
                max_connections=32,
                max_keepalive_connections=8,
                keepalive_expiry=120.0,
            ),
            headers={"User-Agent": "Jarvis/0.2"},
        )
    return _client


def _endpoint_url() -> str:
    base = SETTINGS.llm.base_url.rstrip("/")
    if SETTINGS.llm.provider == "minimax":
        return f"{base}/text/chatcompletion_v2"
    return f"{base}/chat/completions"


class ChatStream:
    """Single-turn streaming chat. Owns per-turn delta buffers."""

    def __init__(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None,
        tools: list[dict[str, Any]] | None,
        temperature: float | None,
        timeout: float,
    ) -> None:
        self._messages = messages
        self._model = model or SETTINGS.llm.model
        self._tools = tools
        self._temperature = temperature
        self._timeout = timeout
        self._tool_slots: dict[int, dict[str, Any]] = {}
        self._assistant = AssistantMessage()

    @property
    def assistant(self) -> AssistantMessage:
        return self._assistant

    async def __aiter__(self) -> AsyncIterator[tuple[str, Any]]:
        url = _endpoint_url()
        headers = {
            "Authorization": f"Bearer {SETTINGS.llm.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        body: dict[str, Any] = {
            "model": self._model,
            "messages": self._messages,
            "stream": True,
        }
        if SETTINGS.llm.provider == "minimax":
            body["thinking"] = {"type": SETTINGS.llm.thinking}
        if self._tools:
            body["tools"] = self._tools
            body["tool_choice"] = "auto"
        if self._temperature is not None:
            body["temperature"] = self._temperature

        client = _get_client()
        attempts = 0
        emitted = False
        while True:
            try:
                async with client.stream(
                    "POST", url, json=body, headers=headers, timeout=self._timeout
                ) as resp:
                    if resp.status_code >= 400:
                        text = await resp.aread()
                        if not emitted and attempts < _MAX_RETRIES and resp.status_code in _RETRYABLE_STATUS:
                            delay = _retry_delay(attempts, resp.headers.get("Retry-After"))
                            attempts += 1
                            await BUS.publish(
                                "llm_retry",
                                {
                                    "attempt": attempts,
                                    "reason": f"HTTP {resp.status_code}",
                                    "delay": round(delay, 2),
                                },
                            )
                            await asyncio.sleep(delay)
                            continue
                        raise LLMError(f"LLM HTTP {resp.status_code}: {text.decode(errors='replace')[:500]}")
                    async for raw in resp.aiter_lines():
                        if not raw or raw.startswith(":"):
                            continue
                        payload = raw[5:].strip() if raw.startswith("data:") else raw.strip()
                        if not payload:
                            continue
                        if payload == "[DONE]":
                            break
                        try:
                            evt = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        # MiniMax reports errors via base_resp even on HTTP 200.
                        base_resp = evt.get("base_resp") or {}
                        if base_resp.get("status_code", 0) not in (0, None):
                            raise LLMError(
                                f"LLM error {base_resp.get('status_code')}: {base_resp.get('status_msg')}"
                            )
                        for choice in evt.get("choices") or []:
                            delta = choice.get("delta") or {}
                            message = choice.get("message") or {}
                            finish = choice.get("finish_reason")
                            if finish:
                                self._assistant.finish_reason = str(finish)
                                emitted = True
                                yield "finish", str(finish)
                            reasoning = (
                                delta.get("reasoning_content") or message.get("reasoning_content") or ""
                            )
                            if reasoning:
                                emitted = True
                                yield "reasoning", reasoning
                            content = delta.get("content") or message.get("content") or ""
                            if content:
                                new_part = self._absorb_content_delta(content)
                                if new_part:
                                    self._assistant.content += new_part
                                    emitted = True
                                    yield "delta", new_part
                            for tc in delta.get("tool_calls") or []:
                                self._absorb_tool_delta(tc)
                            for tc in message.get("tool_calls") or []:
                                self._absorb_tool_delta(tc)
            except httpx.HTTPError as exc:
                # Retries are safe only before the first emitted event — once
                # the UI has seen tokens, a retry would duplicate the answer.
                if not emitted and attempts < _MAX_RETRIES:
                    delay = _retry_delay(attempts, None)
                    attempts += 1
                    await BUS.publish(
                        "llm_retry",
                        {
                            "attempt": attempts,
                            "reason": f"{type(exc).__name__}: {str(exc)[:120]}",
                            "delay": round(delay, 2),
                        },
                    )
                    await asyncio.sleep(delay)
                    continue
                raise LLMError(f"LLM network error: {exc}") from exc
            break

        self._finalize_tool_calls()
        yield "done", self._assistant.to_openai()

    def _absorb_content_delta(self, content: str) -> str:
        """Return only the portion of ``content`` not already in the buffer.

        MiniMax-M3 (and similar models) sometimes emit the same text
        multiple times in a single chunk or across consecutive chunks.
        Falls back to returning the chunk as-is when no overlap is detectable.
        """
        if not content:
            return ""
        cur = self._assistant.content
        n = len(content)
        if n >= 4 and n % 2 == 0:
            half = n // 2
            if content[:half] == content[half:]:
                content = content[:half]
        if not cur:
            return content
        if cur.endswith(content):
            return ""
        if content.startswith(cur):
            return content[len(cur) :]
        max_check = min(len(cur), len(content), 200)
        for i in range(max_check, 0, -1):
            if cur[-i:] == content[:i]:
                return content[i:]
        return content

    def _absorb_tool_delta(self, tc: dict[str, Any]) -> None:
        idx = tc.get("index", 0)
        slot = self._tool_slots.setdefault(idx, {"id": None, "name": "", "arguments": ""})
        if tc.get("id"):
            slot["id"] = tc["id"]
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        args = fn.get("arguments") or ""
        # MiniMax streams fragments, then repeats the full call in the final
        # chunk. Only append when the incoming piece is not already a suffix.
        if name and not (slot["name"] and slot["name"].endswith(name)):
            slot["name"] += name
        if not args:
            return
        cur = slot["arguments"]
        if not cur:
            slot["arguments"] = args
        elif cur.endswith(args):
            pass
        elif args.startswith(cur):
            slot["arguments"] = args
        else:
            slot["arguments"] = cur + args

    def _finalize_tool_calls(self) -> None:
        calls: list[dict[str, Any]] = []
        for idx in sorted(self._tool_slots):
            slot = self._tool_slots[idx]
            raw = (slot.get("arguments") or "").strip()
            try:
                args = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                args = {"_raw": raw}
            calls.append(
                {
                    "id": slot.get("id") or f"call_{idx}",
                    "type": "function",
                    "function": {
                        "name": slot.get("name") or "",
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }
            )
        self._assistant.tool_calls = calls


_THINKING_MARKER = re.compile(r"(?:^|\n)\s*response\s*(?=\n|$)")
_MAX_NO_MARKER = 4000


class _ThinkingStripper:
    """Fallback for providers that inline the thinking block into `content`.

    Only used when thinking is actually enabled and the stream carries no
    native `reasoning_content` field. Bounded: if the marker does not show
    up within ``_MAX_NO_MARKER`` characters we assume there is no thinking
    block and flush everything — the answer must never be held hostage.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_answer = False

    def feed(self, chunk: str) -> str:
        if self._in_answer:
            return chunk
        self._buf += chunk
        m = _THINKING_MARKER.search(self._buf)
        if m:
            self._in_answer = True
            rest = self._buf[m.end() :]
            self._buf = ""
            return rest
        if len(self._buf) > _MAX_NO_MARKER:
            self._in_answer = True
            out = self._buf
            self._buf = ""
            return out
        return ""

    def drain(self) -> str:
        out = self._buf
        self._buf = ""
        return out


def _thinking_active() -> bool:
    return SETTINGS.llm.provider == "minimax" and SETTINGS.llm.thinking != "disabled"


async def stream_clean(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    temperature: float | None = None,
    timeout: float = 120.0,
):
    """Stream a chat turn, yielding ``delta`` / ``reasoning`` / ``finish`` /
    ``done`` events.

    ``delta`` payloads always contain final-answer text only: native
    ``reasoning_content`` is routed to ``reasoning`` events, and the
    inline-marker fallback stripper is applied only when thinking is
    enabled. With thinking disabled (the default in `.env`) tokens pass
    through with zero buffering, so the UI sees the first token instantly.
    """
    is_local = bool(model and model.startswith("local:"))
    stripper = _ThinkingStripper() if (not is_local and _thinking_active()) else None
    stream = stream_chat(messages, model=model, tools=tools, temperature=temperature, timeout=timeout)
    saw_reasoning = False
    async for kind, value in stream:
        if kind == "delta":
            if stripper is not None:
                if saw_reasoning:
                    yield "delta", str(value)
                    continue
                clean = stripper.feed(str(value))
                if clean:
                    yield "delta", clean
            else:
                yield "delta", str(value)
        elif kind == "reasoning":
            saw_reasoning = True
            yield "reasoning", value
        elif kind == "finish":
            yield "finish", value
        elif kind == "done":
            if stripper is not None and not saw_reasoning:
                tail = stripper.drain()
                if tail:
                    yield "delta", tail
            if hasattr(stream, "assistant"):
                yield "done", stream.assistant.to_openai()
            else:
                yield "done", value


def stream_chat(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    temperature: float | None = None,
    timeout: float = 120.0,
) -> ChatStream | _LocalStreamAdapter:
    """Pick the right backend for the requested model.

    Model ids prefixed with ``local:`` route to the on-device runner
    (see `jarvis/local_models.py`). Everything else goes through the
    OpenAI-compat HTTP client. Both backends yield the same
    ``delta / reasoning / finish / done`` event protocol.
    """
    if model and model.startswith("local:"):
        local_id = model[len("local:") :]
        return _LocalStreamAdapter(local_id, messages, tools=tools, temperature=temperature)
    return ChatStream(
        messages,
        model=model,
        tools=tools,
        temperature=temperature,
        timeout=timeout,
    )


class _LocalStreamAdapter:
    """Adapts `LocalModelRunner.stream_chat` to the protocol of `ChatStream`."""

    def __init__(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        temperature: float | None,
    ) -> None:
        self._model_id = model_id
        self._messages = messages
        self._tools = tools
        self._temperature = temperature
        self._assistant = AssistantMessage()

    @property
    def assistant(self) -> AssistantMessage:
        return self._assistant

    async def __aiter__(self) -> AsyncIterator[tuple[str, Any]]:
        from .local_models import RUNNER

        async for kind, value in RUNNER.stream_chat(
            self._model_id,
            self._messages,
            tools=self._tools,
            temperature=self._temperature,
        ):
            if kind == "delta":
                self._assistant.content += str(value)
                yield "delta", value
            elif kind == "reasoning":
                yield "reasoning", value
            elif kind == "finish":
                self._assistant.finish_reason = str(value)
                yield "finish", value
            elif kind == "done":
                self._assistant.tool_calls = value.get("tool_calls") or []
                yield "done", self._assistant.to_openai()
