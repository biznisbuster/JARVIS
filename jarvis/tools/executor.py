"""The single authoritative boundary for executing model-selected tools."""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

from ..bus import BUS
from .base import ToolErrorCode, ToolErrorCodeValue, ToolResult, ToolSpec
from .registry import DEFAULT_REGISTRY, ToolRegistry


class PermissionChecker(Protocol):
    async def check(self, tool: str, args: dict[str, Any] | None = None) -> bool: ...


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Narrow execution context; the executor never receives a Session."""

    session_id: str
    permission_store: PermissionChecker


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """Result of one call, including the one terminal event to publish."""

    call_id: str
    name: str
    result: ToolResult
    terminal_event: str
    terminal_payload: dict[str, Any]

    def tool_message(self) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": self.call_id,
            "name": self.name,
            "content": self.result.to_json(),
        }


SpeechSuppressor: TypeAlias = Callable[[str], Any]


class ToolExecutor:
    """Parse, validate, authorize, execute and normalize one tool call."""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        *,
        default_timeout_s: float = 30.0,
        bus: Any = BUS,
        speech_suppressor: SpeechSuppressor | None = None,
    ) -> None:
        self.registry = registry if registry is not None else DEFAULT_REGISTRY
        self.default_timeout_s = default_timeout_s
        self.bus = bus
        self.speech_suppressor = speech_suppressor

    async def execute_call(
        self,
        call: Mapping[str, Any] | Any,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecution:
        call_id = self._call_id(call)
        function = call.get("function") if isinstance(call, Mapping) else None
        function = function if isinstance(function, Mapping) else {}
        raw_name = function.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) else ""

        if not name:
            return await self._executor_failure(
                context,
                call_id,
                name,
                ToolErrorCode.INVALID_ARGUMENTS,
                "tool call is missing a function name",
            )

        args, parse_error = self._parse_arguments(function.get("arguments"))
        if parse_error is not None:
            return await self._executor_failure(
                context,
                call_id,
                name,
                ToolErrorCode.INVALID_ARGUMENTS,
                parse_error,
            )

        definition = self.registry.get(name)
        if definition is None:
            return await self._executor_failure(
                context,
                call_id,
                name,
                ToolErrorCode.NOT_AVAILABLE,
                f"unknown tool: {name}",
            )

        validation_error = self._validate_arguments(args, definition.schema)
        if validation_error is not None:
            return await self._executor_failure(
                context,
                call_id,
                name,
                ToolErrorCode.INVALID_ARGUMENTS,
                validation_error,
            )

        await self.bus.publish(
            "tool_call",
            {"session": context.session_id, "tool": name, "args": args, "call_id": call_id},
        )

        try:
            allowed = await context.permission_store.check(name, args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return await self._executor_exception(context, call_id, name, exc)

        if not allowed:
            result = ToolResult.failure(
                ToolErrorCode.PERMISSION_DENIED,
                f"denied by permission policy for tool '{name}'",
                data={"denied": True},
            )
            return await self._structured_execution(context, call_id, name, result, denied=True)

        if definition.suppresses_speech and self.speech_suppressor is not None:
            try:
                suppression_result = self.speech_suppressor(context.session_id)
                if inspect.isawaitable(suppression_result):
                    await suppression_result
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                return await self._executor_exception(context, call_id, name, exc)

        timeout_s = self._resolve_timeout(definition, args)
        try:
            if timeout_s is None:
                raw_result = await definition.execute(args)
            else:
                raw_result = await asyncio.wait_for(definition.execute(args), timeout=timeout_s)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return await self._executor_failure(
                context,
                call_id,
                name,
                ToolErrorCode.TIMEOUT,
                f"tool '{name}' timed out after {timeout_s:g}s",
            )
        except FileNotFoundError as exc:
            return await self._executor_failure(
                context,
                call_id,
                name,
                ToolErrorCode.DEPENDENCY_MISSING,
                f"required executable is unavailable: {exc}",
            )
        except ModuleNotFoundError as exc:
            missing = getattr(exc, "name", None) or str(exc)
            return await self._executor_failure(
                context,
                call_id,
                name,
                ToolErrorCode.DEPENDENCY_MISSING,
                f"optional dependency is unavailable: {missing}",
            )
        except Exception as exc:  # noqa: BLE001
            return await self._executor_exception(context, call_id, name, exc)

        result = self._normalize_result(raw_result)
        return await self._structured_execution(context, call_id, name, result)

    async def _executor_exception(
        self,
        context: ToolExecutionContext,
        call_id: str,
        name: str,
        exc: Exception,
    ) -> ToolExecution:
        code = getattr(exc, "error_code", None)
        if isinstance(code, (ToolErrorCode, str)) and str(code):
            return await self._executor_failure(context, call_id, name, code, str(exc))
        return await self._executor_failure(
            context,
            call_id,
            name,
            ToolErrorCode.EXECUTION_FAILED,
            str(exc) or type(exc).__name__,
        )

    async def _executor_failure(
        self,
        context: ToolExecutionContext,
        call_id: str,
        name: str,
        code: ToolErrorCodeValue,
        message: str,
    ) -> ToolExecution:
        result = ToolResult.failure(code, message)
        payload = {
            "session": context.session_id,
            "tool": name,
            "call_id": call_id,
            "error_code": result.error.code_value if result.error else str(code),
            "error": message,
        }
        await self.bus.publish("tool_error", payload)
        return ToolExecution(call_id, name, result, "tool_error", payload)

    async def _structured_execution(
        self,
        context: ToolExecutionContext,
        call_id: str,
        name: str,
        result: ToolResult,
        *,
        denied: bool = False,
    ) -> ToolExecution:
        payload = {"session": context.session_id, "tool": name, "call_id": call_id}
        payload.update(result.to_payload())
        if denied:
            payload["denied"] = True
        await self.bus.publish("tool_done", payload)
        return ToolExecution(call_id, name, result, "tool_done", payload)

    def _normalize_result(self, value: Any) -> ToolResult:
        if isinstance(value, ToolResult):
            if not value.ok and value.error is None:
                return ToolResult.failure(
                    ToolErrorCode.EXECUTION_FAILED,
                    "tool returned an unsuccessful result without an error",
                    data=value.data,
                    meta=value.meta,
                )
            return value
        default_code = ToolErrorCode.EXECUTION_FAILED
        if isinstance(value, Mapping) and value.get("ok") is False:
            if value.get("error_code") is None and (
                "delivered" in value or "verified" in value or "verification" in value
            ):
                default_code = ToolErrorCode.VERIFICATION_FAILED
        return ToolResult.from_legacy(value, default_error_code=default_code)

    @staticmethod
    def _call_id(call: Any) -> str:
        value = call.get("id") if isinstance(call, Mapping) else None
        if isinstance(value, str) and value:
            return value
        return uuid.uuid4().hex[:10]

    @staticmethod
    def _parse_arguments(raw_arguments: Any) -> tuple[dict[str, Any], str | None]:
        if raw_arguments is None or raw_arguments == "":
            return {}, None
        if isinstance(raw_arguments, str):
            try:
                parsed = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                return {}, f"invalid tool arguments JSON: {exc.msg}"
        elif isinstance(raw_arguments, Mapping):
            parsed = dict(raw_arguments)
        else:
            return {}, "tool arguments must be a JSON object"
        if not isinstance(parsed, dict):
            return {}, "tool arguments must be a JSON object"
        return parsed, None

    @staticmethod
    def _validate_arguments(args: dict[str, Any], schema: Mapping[str, Any]) -> str | None:
        function = schema.get("function") if isinstance(schema, Mapping) else None
        parameters = function.get("parameters") if isinstance(function, Mapping) else None
        if not isinstance(parameters, Mapping):
            return None
        properties = parameters.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        required = parameters.get("required")
        for field in required if isinstance(required, list) else []:
            if field not in args:
                return f"missing required argument: {field}"

        for name, value in args.items():
            definition = properties.get(name)
            if not isinstance(definition, Mapping):
                continue
            expected = definition.get("type")
            if expected == "string" and not isinstance(value, str):
                return f"argument '{name}' must be a string"
            if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
                return f"argument '{name}' must be an integer"
            if expected == "boolean" and not isinstance(value, bool):
                return f"argument '{name}' must be a boolean"
            if expected == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
                return f"argument '{name}' must be a number"
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                minimum = definition.get("minimum")
                maximum = definition.get("maximum")
                if minimum is not None and value < minimum:
                    return f"argument '{name}' must be >= {minimum}"
                if maximum is not None and value > maximum:
                    return f"argument '{name}' must be <= {maximum}"
        return None

    def _resolve_timeout(self, definition: ToolSpec, args: dict[str, Any]) -> float | None:
        if definition.name == "kilo_run":
            requested = args.get("max_duration_s")
            if isinstance(requested, (int, float)) and not isinstance(requested, bool) and requested > 0:
                return float(requested) + 5.0
            return 185.0
        if definition.timeout_s is not None:
            return max(float(definition.timeout_s), 0.001)
        return max(float(self.default_timeout_s), 0.001)
