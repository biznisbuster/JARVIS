"""Canonical JARVIS tool package.

New code should depend on ``DEFAULT_REGISTRY``/``ToolExecutor`` rather than
the historical ``jarvis.agent.tools`` module.
"""

from .base import ToolDef, ToolError, ToolErrorCode, ToolResult, ToolSpec
from .executor import ToolExecution, ToolExecutionContext, ToolExecutor
from .registry import DEFAULT_REGISTRY, ToolRegistry, all_schemas, get

__all__ = [
    "DEFAULT_REGISTRY",
    "ToolDef",
    "ToolError",
    "ToolErrorCode",
    "ToolExecution",
    "ToolExecutionContext",
    "ToolExecutor",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "all_schemas",
    "get",
]
