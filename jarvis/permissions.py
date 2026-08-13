"""Permission engine.

Three states per tool: `allow` | `ask` | `deny`. Unknown tools fall back to
the store's default policy. The permission set lives in
`config/permissions.json` and is hot-reloadable from the UI.

Tool execution flow:

    tools.execute(...) -> Permission.check(tool, args) -> allow|ask|deny
                                                       |
                                                       v
                                              approval gate (UI prompt)
                                                       |
                                                       v
                                                  run or block
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bus import BUS


@dataclass
class Permission:
    tool: str
    action: str
    args_summary: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)


class PermissionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {"default_policy": "ask", "tools": {}}
        self._pending: dict[str, tuple[Permission, asyncio.Future[dict[str, Any]]]] = {}
        self.reload()

    # ---- persistence ----
    def reload(self) -> None:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"default_policy": "ask", "tools": {}}, indent=2))
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            data = {"default_policy": "ask", "tools": {}}
        self._data = {
            "default_policy": data.get("default_policy", "ask"),
            "tools": dict(data.get("tools") or {}),
        }

    def persist(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False))
        tmp.replace(self.path)

    # ---- queries ----
    def get_default(self) -> str:
        return self._data.get("default_policy", "ask")

    def get_policy(self, tool: str) -> str:
        return self._data.get("tools", {}).get(tool, self.get_default())

    def snapshot(self) -> dict[str, Any]:
        return {
            "default_policy": self.get_default(),
            "tools": dict(self._data.get("tools") or {}),
            "path": str(self.path),
        }

    def set_default(self, policy: str) -> None:
        self._data["default_policy"] = policy
        self.persist()

    def set_policy(self, tool: str, policy: str) -> None:
        self._data.setdefault("tools", {})[tool] = policy
        self.persist()

    def reset_tools(self) -> None:
        self._data["tools"] = {}
        self.persist()

    # ---- runtime gate ----
    async def check(self, tool: str, args: dict[str, Any] | None = None) -> bool:
        args = args or {}
        policy = self.get_policy(tool)
        if policy == "allow":
            await BUS.publish("permission", {"tool": tool, "action": "allow", "args": args})
            return True
        if policy == "deny":
            await BUS.publish("permission", {"tool": tool, "action": "deny", "args": args})
            return False
        # ask
        return await self._prompt_user(tool, args)

    async def _prompt_user(self, tool: str, args: dict[str, Any]) -> bool:
        req = Permission(tool=tool, action="ask", args=args, args_summary=_summarize(args))
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[req.request_id] = (req, fut)
        await BUS.publish("permission_request", _serialize(req))
        try:
            decision = await asyncio.wait_for(fut, timeout=300)
        except TimeoutError:
            decision = {"action": "deny", "remember": False}
        finally:
            self._pending.pop(req.request_id, None)

        action = decision.get("action", "deny")
        remember = bool(decision.get("remember"))
        if remember and action in ("allow", "deny"):
            self.set_policy(tool, action)
        await BUS.publish(
            "permission", {"tool": tool, "action": action, "args": args, "remembered": remember}
        )
        return action == "allow"

    def resolve(self, request_id: str, action: str, remember: bool = False) -> bool:
        entry = self._pending.get(request_id)
        if entry is None:
            return False
        _, fut = entry
        if fut.done():
            return False
        fut.set_result({"action": action, "remember": remember})
        return True

    def list_pending(self) -> list[dict[str, Any]]:
        return [
            {"request_id": k, "tool": req.tool, "args": req.args, "summary": req.args_summary}
            for k, (req, _) in list(self._pending.items())
        ]


def _summarize(args: dict[str, Any]) -> str:
    parts: list[str] = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 120:
            s = s[:117] + "..."
        parts.append(f"{k}={s}")
    return ", ".join(parts)[:400]


def _serialize(p: Permission) -> dict[str, Any]:
    return {
        "request_id": p.request_id,
        "tool": p.tool,
        "action": p.action,
        "args": p.args,
        "summary": p.args_summary,
        "created_at": p.created_at,
    }
