from ovid_core.errors import ToolExecutionError
from ovid_core.models import BaseModel
from ovid_core.tools.base import ToolExecutionContext
from ovid_core.tools.models import ToolResult


class BaseToolHook[Deps]:
    async def before_tool(
        self,
        context: ToolExecutionContext[Deps],
        tool_id: str,
        arguments: BaseModel,
    ) -> None:
        pass

    async def after_tool(
        self,
        context: ToolExecutionContext[Deps],
        tool_id: str,
        result: ToolResult,
    ) -> None:
        pass

    async def on_tool_error(
        self,
        context: ToolExecutionContext[Deps],
        tool_id: str,
        error: ToolExecutionError,
    ) -> None:
        pass
