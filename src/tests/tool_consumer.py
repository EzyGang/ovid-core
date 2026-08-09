import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Self, cast

from ovid_core.capabilities.base import BaseCapability, CapabilityContributions, CapabilityModelSettings
from ovid_core.errors import ToolExecutionError
from ovid_core.hooks.base import BaseToolHook
from ovid_core.models import BaseModel
from ovid_core.runtime.context import RunContext
from ovid_core.tools.base import BaseTool, BaseToolset, ToolExecutionContext
from ovid_core.tools.models import ToolApproval, ToolResult


@dataclass(frozen=True, slots=True)
class Dependencies:
    prefix: str


class AddArguments(BaseModel):
    left: int
    right: int


class AddTool(BaseTool[Dependencies, AddArguments, ToolResult]):
    id = 'add'
    description = 'Add two integers'
    args_type = AddArguments
    result_type = ToolResult
    approval = ToolApproval(required=True, reason='Writes a total', metadata={'risk': 'low'})

    def __init__(self) -> None:
        self.context: ToolExecutionContext[Dependencies] | None = None

    async def execute(
        self,
        context: ToolExecutionContext[Dependencies],
        arguments: AddArguments,
    ) -> ToolResult:
        self.context = context
        return ToolResult(content=arguments.left + arguments.right, metadata={'prefix': context.run.deps.prefix})


class FastAddTool(AddTool):
    id = 'fast_add'
    approval = ToolApproval()


class ControlledTool(AddTool):
    id = 'controlled'
    approval = ToolApproval()

    def __init__(self, *, mode: str, timeout_seconds: float | None = None) -> None:
        super().__init__()
        self.mode = mode
        self.timeout_seconds = timeout_seconds
        self.cancelled = False

    async def execute(
        self,
        context: ToolExecutionContext[Dependencies],
        arguments: AddArguments,
    ) -> ToolResult:
        if self.mode == 'error':
            raise ValueError('consumer secret')
        if self.mode == 'typed_error':
            raise ToolExecutionError('controlled failure')
        if self.mode == 'invalid_result':
            return cast(ToolResult, 'invalid')

        try:
            await asyncio.sleep(10)
        finally:
            self.cancelled = True
        return await super().execute(context, arguments)


class TrackingToolset(BaseToolset[Dependencies]):
    id = 'arithmetic'

    def __init__(
        self,
        tools: tuple[BaseTool[Dependencies, AddArguments, ToolResult], ...],
        *,
        replace_on_step: bool = False,
    ) -> None:
        self.tools = tools
        self.replace_on_step = replace_on_step
        self.entered = 0
        self.exited = 0
        self.steps = 0

    async def for_step(self, context: RunContext[Dependencies]) -> Self:
        del context
        self.steps += 1
        return type(self)(self.tools) if self.replace_on_step else self

    async def __aenter__(self) -> Self:
        self.entered += 1
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        del exception_type, exception, traceback
        self.exited += 1
        return None

    async def get_tools(
        self,
        context: RunContext[Dependencies],
    ) -> Sequence[BaseTool[Dependencies, AddArguments, ToolResult]]:
        del context
        return self.tools


class RecordingHook(BaseToolHook[Dependencies]):
    def __init__(self) -> None:
        self.events: list[str] = []

    async def before_tool(
        self,
        context: ToolExecutionContext[Dependencies],
        tool_id: str,
        arguments: BaseModel,
    ) -> None:
        del context, arguments
        self.events.append(f'before:{tool_id}')

    async def after_tool(
        self,
        context: ToolExecutionContext[Dependencies],
        tool_id: str,
        result: ToolResult,
    ) -> None:
        del context, result
        self.events.append(f'after:{tool_id}')

    async def on_tool_error(
        self,
        context: ToolExecutionContext[Dependencies],
        tool_id: str,
        error: ToolExecutionError,
    ) -> None:
        del context, error
        self.events.append(f'error:{tool_id}')


def arithmetic_capability(tool: AddTool, toolset: TrackingToolset, hook: RecordingHook) -> BaseCapability[Dependencies]:
    return BaseCapability(
        id='arithmetic',
        contributions=CapabilityContributions(
            instructions=('Use arithmetic when needed.',),
            tools=(tool,),
            toolsets=(toolset,),
            hooks=(hook,),
            model_settings=CapabilityModelSettings(values={'temperature': 0}),
        ),
    )
