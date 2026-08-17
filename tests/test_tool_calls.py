"""Focused tests for the shared streamed tool-call accumulator."""

from __future__ import annotations

from jarvis.tool_calls import ToolCallAccumulator


def _delta(index: int | None, *, call_id: str | None = None, name: str = "", arguments: object = "") -> dict:
    delta = {"function": {}}
    if index is not None:
        delta["index"] = index
    if call_id is not None:
        delta["id"] = call_id
    if name:
        delta["function"]["name"] = name
    if arguments != "":
        delta["function"]["arguments"] = arguments
    return delta


def test_complete_call_is_openai_compatible() -> None:
    acc = ToolCallAccumulator()
    acc.absorb(_delta(0, call_id="call-0", name="time_now", arguments={"zone": "local"}))

    assert acc.finalize() == [
        {
            "id": "call-0",
            "type": "function",
            "function": {"name": "time_now", "arguments": '{"zone": "local"}'},
        }
    ]


def test_fragmented_name_and_arguments_are_one_call() -> None:
    acc = ToolCallAccumulator()
    acc.absorb(_delta(0, call_id="call-0", name="rem", arguments='{"query":'))
    acc.absorb(_delta(0, name="inders_create", arguments=' "x"}'))

    calls = acc.finalize()
    assert len(calls) == 1
    assert calls[0]["function"] == {"name": "reminders_create", "arguments": '{"query": "x"}'}


def test_repeated_full_call_does_not_duplicate_name_or_arguments() -> None:
    acc = ToolCallAccumulator()
    acc.absorb(_delta(0, call_id="call-0", name="rem", arguments='{"q":'))
    acc.absorb(_delta(0, name="inders_create", arguments=' "x"}'))
    acc.absorb(_delta(0, name="reminders_create", arguments='{"q": "x"}'))

    assert acc.finalize()[0]["function"] == {"name": "reminders_create", "arguments": '{"q": "x"}'}


def test_full_json_repetition_can_change_whitespace_without_duplication() -> None:
    acc = ToolCallAccumulator()
    acc.absorb(_delta(0, call_id="call-0", name="time_now", arguments='{"q":'))
    acc.absorb(_delta(0, arguments='{ "q" : "x" }'))

    assert acc.finalize()[0]["function"]["arguments"] == '{"q": "x"}'


def test_interleaved_calls_are_ordered_by_index() -> None:
    acc = ToolCallAccumulator()
    acc.absorb(_delta(1, call_id="call-1", name="time", arguments=""))
    acc.absorb(_delta(0, call_id="call-0", name="rem", arguments='{"title":'))
    acc.absorb(_delta(1, name="_now", arguments={}))
    acc.absorb(_delta(0, name="inders_create", arguments=' "x"}'))

    assert [call["id"] for call in acc.finalize()] == ["call-0", "call-1"]
    assert acc.finalize()[1]["function"] == {"name": "time_now", "arguments": "{}"}


def test_missing_index_uses_a_stable_deterministic_slot() -> None:
    acc = ToolCallAccumulator()
    acc.absorb(_delta(None, call_id="call-0", name="rem", arguments='{"q":'))
    acc.absorb(_delta(None, name="inders_create", arguments=' "x"}'))

    assert acc.finalize()[0]["id"] == "call-0"
    assert acc.finalize()[0]["function"]["name"] == "reminders_create"


def test_missing_id_gets_stable_index_based_id() -> None:
    acc = ToolCallAccumulator()
    acc.absorb(_delta(2, name="time_now", arguments={}))

    first = acc.finalize()
    second = acc.finalize()
    assert first[0]["id"] == "call_2"
    assert second == first


def test_invalid_json_is_preserved_and_reported() -> None:
    acc = ToolCallAccumulator()
    acc.absorb(_delta(0, call_id="bad", name="time_now", arguments='{"broken"'))

    calls = acc.finalize()
    assert calls[0]["function"]["arguments"] == '{"broken"'
    assert "bad" in acc.parse_errors
    assert "invalid tool arguments JSON" in acc.parse_errors["bad"]


def test_empty_arguments_finalize_to_empty_object() -> None:
    acc = ToolCallAccumulator()
    acc.absorb(_delta(0, call_id="empty", name="time_now"))

    assert acc.finalize()[0]["function"]["arguments"] == "{}"


def test_empty_function_name_is_not_executable() -> None:
    acc = ToolCallAccumulator()
    acc.absorb(_delta(0, call_id="missing-name", arguments="{}"))

    assert acc.finalize() == []
    assert acc.parse_errors["missing-name"] == "tool call is missing a function name"
