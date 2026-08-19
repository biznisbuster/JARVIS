"""Canonical types for JARVIS tool definitions and execution results."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeAlias


class ToolErrorCode(StrEnum):
    """Small, stable vocabulary for executor-visible failures."""

    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    NOT_READY = "NOT_READY"
    TIMEOUT = "TIMEOUT"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    CANCELLED = "CANCELLED"


# Media and other domain adapters already expose a few useful, more specific
# error codes.  They remain strings at the boundary so the executor can
# preserve them without growing a central error hierarchy.
ToolErrorCodeValue: TypeAlias = ToolErrorCode | str


@dataclass(frozen=True, slots=True)
class ToolError:
    """A machine-readable tool failure with concise user-facing details."""

    code: ToolErrorCodeValue
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def code_value(self) -> str:
        return self.code.value if isinstance(self.code, ToolErrorCode) else str(self.code)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code_value,
            "message": self.message,
        }
        if self.details:
            result["details"] = dict(self.details)
        return result


ToolCallable: TypeAlias = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Canonical internal result while retaining the old flat JSON shape.

    ``data`` contains model-visible domain fields such as media verification
    evidence.  ``meta`` is reserved for executor metadata.  Serialization
    keeps domain fields at the top level for compatibility and nests only the
    optional executor metadata under ``meta``.
    """

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: ToolError | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(
        cls,
        data: Mapping[str, Any] | None = None,
        *,
        meta: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        return cls(ok=True, data=dict(data or {}), meta=dict(meta or {}))

    @classmethod
    def failure(
        cls,
        code: ToolErrorCodeValue,
        message: str,
        *,
        data: Mapping[str, Any] | None = None,
        details: Mapping[str, Any] | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        return cls(
            ok=False,
            data=dict(data or {}),
            error=ToolError(code=code, message=message, details=dict(details or {})),
            meta=dict(meta or {}),
        )

    def to_payload(self) -> dict[str, Any]:
        """Return the flat, model-visible payload used by current callers."""

        payload: dict[str, Any] = {"ok": self.ok}
        payload.update(self.data)
        if self.meta:
            payload["meta"] = dict(self.meta)
        if self.error is not None:
            payload["error_code"] = self.error.code_value
            payload["error"] = self.error.message
            if self.error.details:
                payload["error_details"] = dict(self.error.details)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False, default=str)

    @classmethod
    def from_legacy(
        cls,
        value: ToolResult | Mapping[str, Any] | str,
        *,
        default_error_code: ToolErrorCodeValue = ToolErrorCode.EXECUTION_FAILED,
    ) -> ToolResult:
        """Normalize a current result, legacy dict, or legacy JSON string.

        Registered domain functions are being migrated incrementally, so this
        boundary deliberately accepts their existing JSON-string contract.
        Non-JSON strings are explicit failures rather than false successes.
        """

        if isinstance(value, ToolResult):
            return value

        raw: Any = value
        if isinstance(value, str):
            try:
                raw = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return cls.failure(default_error_code, "tool returned malformed non-JSON output")

        if not isinstance(raw, Mapping):
            return cls.failure(default_error_code, "tool returned a non-object result")

        payload = dict(raw)
        raw_ok = payload.pop("ok", None)
        raw_error_code = payload.pop("error_code", None)
        raw_error = payload.pop("error", None)
        raw_meta = payload.pop("meta", None)
        meta = dict(raw_meta) if isinstance(raw_meta, Mapping) else {}

        if raw_ok is False:
            code: ToolErrorCodeValue = raw_error_code or default_error_code
            message = str(raw_error or "tool execution failed")
            details = payload.pop("error_details", None)
            return cls.failure(
                code,
                message,
                data=payload,
                details=details if isinstance(details, Mapping) else None,
                meta=meta,
            )

        # Some legacy domain payloads contain no ``ok`` key (time_now is one
        # example).  They are successful structured payloads by convention.
        return cls.success(payload, meta=meta)


@dataclass
class ToolSpec:
    """One registered tool and the execution policy attached to it."""

    name: str
    description: str
    schema: dict[str, Any]
    execute: ToolCallable
    timeout_s: float | None = None
    suppresses_speech: bool = False


# ``ToolDef`` is the historical public name.  Keeping it as an alias means
# existing callers construct the canonical object rather than a second type.
ToolDef = ToolSpec


def _schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _str_prop(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _int_prop(description: str, default: int | None = None) -> dict[str, Any]:
    prop: dict[str, Any] = {"type": "integer", "description": description}
    if default is not None:
        prop["default"] = default
    return prop


def _bool_prop(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}
