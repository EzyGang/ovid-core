from ovid_core.errors import ToolExecutionError
from ovid_core.hooks.models import ToolCall, ToolDecision
from ovid_core.tools.base import ToolExecutionContext
from ovid_core.tools.models import ToolResult


class BaseToolHook[Deps]:
    async def before_tool(
        self,
        context: ToolExecutionContext[Deps],
        call: ToolCall,
    ) -> ToolDecision | None:
        return None

    async def after_tool(
        self,
        context: ToolExecutionContext[Deps],
        call: ToolCall,
        result: ToolResult,
    ) -> ToolResult | None:
        return None

    async def on_tool_error(
        self,
        context: ToolExecutionContext[Deps],
        call: ToolCall,
        error: ToolExecutionError,
    ) -> ToolResult | None:
        return None
