"""Provider-neutral assembly of streamed function/tool calls.

OpenAI-compatible providers do not all emit tool calls in the same shape.
Some split a function name or its JSON arguments over several deltas, while
others repeat the complete call after streaming the fragments.  This module
keeps those transport details out of individual provider adapters and exposes
one canonical OpenAI-compatible call shape at finalization time.
"""

from __future__ import annotations

import json
from typing import Any


class ToolCallAccumulator:
    """Accumulate streamed tool-call deltas into finalized calls.

    ``absorb`` accepts an OpenAI/Ollama-style tool-call delta.  ``finalize``
    returns calls ordered by their numeric index.  Malformed argument JSON is
    preserved verbatim in the public call and recorded in ``parse_errors`` so
    the execution boundary can reject it deterministically instead of
    silently turning it into a different object.
    """

    def __init__(self) -> None:
        # Keep the slot shape intentionally small and inspectable.  ChatStream
        # exposes this mapping for backwards-compatible focused unit tests.
        self._slots: dict[int, dict[str, Any]] = {}
        self._implicit_index: int | None = None
        self._next_index = 0
        self.parse_errors: dict[str, str] = {}

    @property
    def slots(self) -> dict[int, dict[str, Any]]:
        """The in-progress slots, useful for diagnostics and tests."""

        return self._slots

    def absorb(self, delta: dict[str, Any]) -> None:
        """Absorb one provider tool-call delta."""

        if not isinstance(delta, dict):
            return

        call_id = delta.get("id")
        call_id = call_id if isinstance(call_id, str) and call_id else None
        explicit_index = self._coerce_index(delta.get("index"))
        index = self._resolve_index(explicit_index, call_id)
        slot = self._slots.setdefault(index, {"id": None, "name": "", "arguments": ""})

        if call_id:
            slot["id"] = call_id

        function = delta.get("function") or {}
        if not isinstance(function, dict):
            function = {}

        name = function.get("name")
        if isinstance(name, str) and name:
            slot["name"] = self._merge_text(slot.get("name") or "", name)

        arguments = function.get("arguments")
        if arguments is not None and arguments != "":
            piece = self._arguments_to_text(arguments)
            slot["arguments"] = self._merge_arguments(slot.get("arguments") or "", piece)

    def finalize(self) -> list[dict[str, Any]]:
        """Return stable, OpenAI-compatible calls ordered by call index."""

        self.parse_errors = {}
        calls: list[dict[str, Any]] = []
        for index in sorted(self._slots):
            slot = self._slots[index]
            name = str(slot.get("name") or "").strip()
            call_id = str(slot.get("id") or f"call_{index}")

            # An empty function name cannot be executable.  Do not expose a
            # fake call to the agent loop; retain an explicit diagnostic for
            # direct accumulator users.
            if not name:
                self.parse_errors[call_id] = "tool call is missing a function name"
                continue

            raw = str(slot.get("arguments") or "").strip()
            if not raw:
                arguments = "{}"
            else:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    # Preserve the malformed text.  Converting it to a
                    # successful-looking object would hide the provider bug.
                    arguments = raw
                    self.parse_errors[call_id] = f"invalid tool arguments JSON: {exc.msg}"
                else:
                    arguments = json.dumps(parsed, ensure_ascii=False)

            calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            )
        return calls

    def _resolve_index(self, explicit_index: int | None, call_id: str | None) -> int:
        if call_id:
            for index, slot in self._slots.items():
                if slot.get("id") == call_id:
                    return index

        if explicit_index is not None:
            self._next_index = max(self._next_index, explicit_index + 1)
            return explicit_index

        # When a provider omits indexes, a fragment with a stable id can still
        # be matched above.  Without an id, re-use the only/most recent
        # implicit slot; allocate a new deterministic index only when there
        # is no reasonable existing association.
        if self._implicit_index is not None and not call_id:
            return self._implicit_index
        if len(self._slots) == 1 and not call_id:
            return next(iter(self._slots))

        while self._next_index in self._slots:
            self._next_index += 1
        self._implicit_index = self._next_index
        index = self._next_index
        self._next_index += 1
        return index

    @staticmethod
    def _coerce_index(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return None
        return None

    @staticmethod
    def _arguments_to_text(arguments: Any) -> str:
        if isinstance(arguments, str):
            return arguments
        return json.dumps(arguments, ensure_ascii=False)

    @classmethod
    def _merge_arguments(cls, current: str, incoming: str) -> str:
        merged = cls._merge_text(current, incoming)
        if not current or not incoming:
            return merged

        # A provider may repeat the completed JSON with different whitespace
        # from the fragments that preceded it.  Compare parsed values first
        # and then use a whitespace-insensitive prefix check for a partial
        # fragment followed by a complete object.
        try:
            if json.loads(current) == json.loads(incoming):
                return incoming
        except json.JSONDecodeError:
            pass
        compact_current = "".join(current.split())
        compact_incoming = "".join(incoming.split())
        if compact_incoming.startswith(compact_current):
            return incoming
        return merged

    @staticmethod
    def _merge_text(current: str, incoming: str) -> str:
        """Merge a fragment or repeated full value without duplicating it."""

        if not current:
            return incoming
        if not incoming or incoming == current:
            return current
        if current.endswith(incoming):
            return current
        if incoming.startswith(current):
            return incoming
        if current.startswith(incoming):
            return current

        max_overlap = min(len(current), len(incoming))
        for size in range(max_overlap, 0, -1):
            if current[-size:] == incoming[:size]:
                return current + incoming[size:]
        return current + incoming
