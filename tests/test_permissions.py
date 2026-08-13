"""Tests for jarvis.permissions — policy lookup, gate, _summarize."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.permissions import PermissionStore, _summarize


def test_default_policy_is_ask(tmp_path: Path) -> None:
    p = PermissionStore(tmp_path / "perms.json")
    assert p.get_default() == "ask"
    assert p.get_policy("any_tool") == "ask"


def test_set_policy_persists(tmp_path: Path) -> None:
    p1 = PermissionStore(tmp_path / "perms.json")
    p1.set_policy("reminders_create", "allow")
    p2 = PermissionStore(tmp_path / "perms.json")
    assert p2.get_policy("reminders_create") == "allow"
    assert p2.get_policy("other_tool") == "ask"


def test_set_default_persists(tmp_path: Path) -> None:
    p1 = PermissionStore(tmp_path / "perms.json")
    p1.set_default("deny")
    p2 = PermissionStore(tmp_path / "perms.json")
    assert p2.get_default() == "deny"
    assert p2.get_policy("anything") == "deny"


def test_reset_tools_clears_only_tools(tmp_path: Path) -> None:
    p = PermissionStore(tmp_path / "perms.json")
    p.set_default("deny")
    p.set_policy("a", "allow")
    p.set_policy("b", "ask")
    p.reset_tools()
    assert p.snapshot()["tools"] == {}
    assert p.get_default() == "deny"


def test_summarize_truncates_long_values() -> None:
    s = _summarize({"q": "x" * 500, "k": "ok"})
    assert "k=ok" in s
    assert "..." in s
    assert len(s) <= 400


def test_summarize_handles_non_string() -> None:
    s = _summarize({"n": 42, "lst": [1, 2, 3]})
    assert "n=42" in s
    assert "lst=[1, 2, 3]" in s


@pytest.mark.asyncio
async def test_check_allow_returns_true(permission_store: PermissionStore) -> None:
    permission_store.set_policy("safe_tool", "allow")
    assert await permission_store.check("safe_tool", {"x": 1}) is True


@pytest.mark.asyncio
async def test_check_deny_returns_false(permission_store: PermissionStore) -> None:
    permission_store.set_policy("danger_tool", "deny")
    assert await permission_store.check("danger_tool") is False


@pytest.mark.asyncio
async def test_check_ask_blocks_until_resolved(permission_store: PermissionStore) -> None:
    task = asyncio.create_task(permission_store.check("needs_approval", {"q": "x"}))

    pending: list[dict] = []
    for _ in range(50):
        pending = permission_store.list_pending()
        if pending:
            break
        await asyncio.sleep(0.01)
    assert pending, "ask should register a pending request"
    assert pending[0]["tool"] == "needs_approval"
    assert pending[0]["summary"] == "q=x"

    assert permission_store.resolve(pending[0]["request_id"], "allow") is True
    assert await task is True
    assert permission_store.list_pending() == []


@pytest.mark.asyncio
async def test_resolve_unknown_returns_false(permission_store: PermissionStore) -> None:
    assert permission_store.resolve("not-a-real-id", "allow") is False


@pytest.mark.asyncio
async def test_corrupt_file_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "perms.json"
    path.write_text("{not json")
    p = PermissionStore(path)
    assert p.get_default() == "ask"
    assert p.get_policy("x") == "ask"
