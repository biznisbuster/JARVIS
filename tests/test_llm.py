"""Tests for jarvis.llm — delta buffering + content dedup helpers."""

from __future__ import annotations

from jarvis.llm import ChatStream, _retry_delay


def test_retry_delay_caps_retry_after() -> None:
    assert _retry_delay(0, "10") == 5.0
    assert _retry_delay(0, "0") == 0.0


def test_retry_delay_caps_negative_retry_after() -> None:
    assert _retry_delay(0, "-1") == 0.0


def test_retry_delay_grows_exponentially() -> None:
    d0 = _retry_delay(0, None)
    d1 = _retry_delay(1, None)
    d2 = _retry_delay(2, None)
    assert d0 < d1 < d2
    assert d0 >= 0.7
    assert d2 < 10.0


def test_retry_delay_jitter_window() -> None:
    samples = [_retry_delay(1, None) for _ in range(20)]
    assert min(samples) < max(samples), "jitter should produce variance"


def _new_stream() -> ChatStream:
    return ChatStream(
        messages=[{"role": "user", "content": "x"}],
        model="m",
        tools=None,
        temperature=None,
        timeout=10.0,
    )


def test_absorb_empty_content_returns_empty() -> None:
    s = _new_stream()
    assert s._absorb_content_delta("") == ""


def test_absorb_returns_full_when_buffer_empty() -> None:
    s = _new_stream()
    assert s._absorb_content_delta("hello") == "hello"


def test_absorb_collapses_exact_double() -> None:
    s = _new_stream()
    assert s._absorb_content_delta("abab") == "ab"
    assert s._absorb_content_delta("abcdabcd") == "abcd"


def _absorb(s: ChatStream, content: str) -> str:
    new_part = s._absorb_content_delta(content)
    if new_part:
        s._assistant.content += new_part
    return new_part


def test_absorb_returns_empty_when_already_absorbed() -> None:
    s = _new_stream()
    assert _absorb(s, "hello") == "hello"
    assert _absorb(s, "hello") == ""


def test_absorb_returns_only_new_suffix() -> None:
    s = _new_stream()
    assert _absorb(s, "hello") == "hello"
    assert _absorb(s, "hello world") == " world"


def test_absorb_uses_overlap_window() -> None:
    s = _new_stream()
    _absorb(s, "the quick brown")
    assert _absorb(s, "quick brown fox") == " fox"


def test_absorb_handles_no_overlap() -> None:
    s = _new_stream()
    _absorb(s, "foo")
    assert _absorb(s, "bar") == "bar"


def test_absorb_tool_delta_streams_name() -> None:
    s = _new_stream()
    s._absorb_tool_delta({"index": 0, "id": "abc", "function": {"name": "rem"}})
    s._absorb_tool_delta({"index": 0, "function": {"name": "inders_create"}})
    assert s._tool_slots[0]["id"] == "abc"
    assert s._tool_slots[0]["name"] == "reminders_create"


def test_absorb_tool_delta_keeps_suffix_when_repeated() -> None:
    s = _new_stream()
    s._absorb_tool_delta({"index": 0, "function": {"name": "reminders_create"}})
    # final chunk repeats whole name — should not duplicate
    s._absorb_tool_delta({"index": 0, "function": {"name": "reminders_create"}})
    assert s._tool_slots[0]["name"] == "reminders_create"


def test_absorb_tool_delta_arguments_appends_then_replaces_on_full() -> None:
    s = _new_stream()
    s._absorb_tool_delta({"index": 0, "function": {"arguments": '{"q":'}})
    s._absorb_tool_delta({"index": 0, "function": {"arguments": ' "x"}'}})
    assert '"q":' in s._tool_slots[0]["arguments"]
    assert '"x"' in s._tool_slots[0]["arguments"]
    s._absorb_tool_delta({"index": 0, "function": {"arguments": '{"q": "x"}'}})
    assert s._tool_slots[0]["arguments"] == '{"q": "x"}'
