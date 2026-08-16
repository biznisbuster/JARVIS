"""Small deterministic Ollama transport fakes for local-model tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Any


class FakeStreamResponse:
    def __init__(self, lines: Iterable[str], *, status_code: int = 200, body: bytes = b"") -> None:
        self._lines = list(lines)
        self.status_code = status_code
        self._body = body

    async def __aenter__(self) -> FakeStreamResponse:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return self._body


class FakeAsyncOllamaClient:
    """Replacement for ``httpx.AsyncClient`` serving a fixed SSE stream."""

    def __init__(self, lines: Iterable[str], **kwargs: Any) -> None:
        self.lines = list(lines)
        self.requests: list[dict[str, Any]] = []

    async def __aenter__(self) -> FakeAsyncOllamaClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def stream(self, method: str, url: str, **kwargs: Any) -> FakeStreamResponse:
        self.requests.append({"method": method, "url": url, "kwargs": kwargs})
        return FakeStreamResponse(self.lines)


class FakeSyncResponse:
    def __init__(self, payload: dict[str, Any], *, status_code: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeSyncOllamaClient:
    """Replacement for ``httpx.Client`` serving a fixed probe response."""

    def __init__(self, response: FakeSyncResponse, **kwargs: Any) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    def __enter__(self) -> FakeSyncOllamaClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> FakeSyncResponse:
        self.requests.append({"url": url, "kwargs": kwargs})
        return self.response
