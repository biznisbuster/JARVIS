"""Tests for jarvis.state_store — atomic key/value persistence."""

from __future__ import annotations

import json

import pytest

from jarvis import state_store


@pytest.mark.asyncio
async def test_set_and_get_round_trip(tmp_data_dir) -> None:
    await state_store.set_state_value("model", "minimax/MiniMax-M3")
    val = await state_store.get_state_value("model")
    assert val == "minimax/MiniMax-M3"


@pytest.mark.asyncio
async def test_get_missing_returns_default(tmp_data_dir) -> None:
    assert await state_store.get_state_value("nope") is None
    assert await state_store.get_state_value("nope", default=42) == 42


@pytest.mark.asyncio
async def test_set_overwrites_without_losing_siblings(tmp_data_dir) -> None:
    await state_store.set_state_value("a", 1)
    await state_store.set_state_value("b", 2)
    await state_store.set_state_value("a", 11)
    assert await state_store.get_state_value("a") == 11
    assert await state_store.get_state_value("b") == 2


@pytest.mark.asyncio
async def test_corrupt_file_returns_empty(tmp_data_dir) -> None:
    (tmp_data_dir / "state.json").write_text("not json")
    assert await state_store.get_state_value("anything") is None


@pytest.mark.asyncio
async def test_set_creates_file_atomically(tmp_data_dir) -> None:
    await state_store.set_state_value("x", [1, 2, 3])
    text = (tmp_data_dir / "state.json").read_text()
    data = json.loads(text)
    assert data["x"] == [1, 2, 3]
    assert not (tmp_data_dir / "state.json.tmp").exists()


@pytest.mark.asyncio
async def test_concurrent_writes_are_serialized(tmp_data_dir) -> None:
    import asyncio

    await asyncio.gather(*(state_store.set_state_value(f"k{i}", i) for i in range(20)))
    for i in range(20):
        assert await state_store.get_state_value(f"k{i}") == i
