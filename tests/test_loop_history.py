"""Tests for jarvis.agent.loop — history helpers used by SessionManager."""

from __future__ import annotations

from jarvis.agent.loop import (
    MAX_HISTORY_MESSAGES,
    _collapse_double,
    _drop_orphans,
    _trim_history,
)


def test_collapse_double_keeps_singles() -> None:
    assert _collapse_double("hello") == "hello"
    assert _collapse_double("ab") == "ab"
    assert _collapse_double("abc") == "abc"


def test_collapse_double_strips_exact_doubles() -> None:
    assert _collapse_double("abab") == "ab"
    assert _collapse_double("hellohello") == "hello"


def test_collapse_double_does_not_touch_partial_overlap() -> None:
    assert _collapse_double("hello world") == "hello world"


def test_drop_orphans_strips_leading_tool_messages() -> None:
    msgs = [
        {"role": "tool", "content": "x"},
        {"role": "tool", "content": "y"},
        {"role": "user", "content": "hi"},
    ]
    _drop_orphans(msgs)
    assert msgs[0]["role"] == "user"


def test_drop_orphans_strips_leading_assistant_with_tool_calls() -> None:
    msgs = [
        {"role": "assistant", "tool_calls": [{"id": "1"}]},
        {"role": "user", "content": "hi"},
    ]
    _drop_orphans(msgs)
    assert msgs[0]["role"] == "user"


def test_drop_orphans_no_op_when_clean() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    _drop_orphans(msgs)
    assert len(msgs) == 2


def test_drop_orphans_empty_list() -> None:
    msgs: list[dict] = []
    _drop_orphans(msgs)
    assert msgs == []


def test_drop_orphans_all_orphans_yields_empty() -> None:
    msgs = [
        {"role": "tool", "content": "x"},
        {"role": "assistant", "tool_calls": [{"id": "1"}]},
    ]
    _drop_orphans(msgs)
    assert msgs == []


def test_trim_history_no_op_when_under_limit() -> None:
    msgs = [{"role": "user", "content": "hi"}]
    _trim_history(msgs)
    assert msgs == [{"role": "user", "content": "hi"}]


def test_trim_history_cuts_on_user_boundary() -> None:
    msgs = []
    for i in range(MAX_HISTORY_MESSAGES + 4):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    _trim_history(msgs)
    assert len(msgs) <= MAX_HISTORY_MESSAGES
    assert msgs[0]["role"] == "user"


def test_trim_history_drops_orphan_tool_block_first() -> None:
    msgs = [{"role": "tool", "content": "stale"}]
    for i in range(MAX_HISTORY_MESSAGES + 4):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    _trim_history(msgs)
    assert msgs[0]["role"] == "user"


def test_trim_history_drops_assistant_tool_calls_block() -> None:
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
    ]
    for i in range(MAX_HISTORY_MESSAGES + 4):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    _trim_history(msgs)
    assert all(m.get("tool_calls") is None for m in msgs[:1])
    assert msgs[0].get("role") == "user"
