import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ovid_core.agents import AgentDefinition, AgentRunPolicy
from ovid_core.capabilities.base import BaseCapability, CapabilityContributions
from ovid_core.hooks.base import BaseToolHook
from ovid_core.models import BaseModel
from ovid_core.routing.models import ModelRef, ModelRouteRef
from ovid_core.runtime.context import RunContext
from ovid_core.tools.base import BaseTool, BaseToolset, ToolExecutionContext
from ovid_core.tools.models import ToolResult


@dataclass(slots=True)
class AgentDependencies:
    prefix: str
    events: list[str] = field(default_factory=list)
    started: asyncio.Event = field(default_factory=asyncio.Event)


class StructuredAnswer(BaseModel):
    value: str


class AddArguments(BaseModel):
    left: int
    right: int


class AddTool(BaseTool[AgentDependencies, AddArguments, ToolResult]):
    id = 'add'
    description = 'Add two integers.'
    args_type = AddArguments
    result_type = ToolResult

    async def execute(
        self,
        context: ToolExecutionContext[AgentDependencies],
        arguments: AddArguments,
    ) -> ToolResult:
        context.run.deps.events.append(f'{context.run.deps.prefix}:add')

        return ToolResult(content=arguments.left + arguments.right)


class WaitArguments(BaseModel):
    value: str


class WaitTool(BaseTool[AgentDependencies, WaitArguments, ToolResult]):
    id = 'wait'
    description = 'Wait until cancelled.'
    args_type = WaitArguments
    result_type = ToolResult

    def __init__(self) -> None:
        self.cancelled = False

    async def execute(
        self,
        context: ToolExecutionContext[AgentDependencies],
        arguments: WaitArguments,
    ) -> ToolResult:
        del arguments
        context.run.deps.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

        raise AssertionError('unreachable')


class ConsumerToolset(BaseToolset[AgentDependencies]):
    id = 'consumer'

    def __init__(self, tools: Sequence[BaseTool[AgentDependencies, Any, Any]]) -> None:
        self._tools = tools

    async def get_tools(
        self,
        context: RunContext[AgentDependencies],
    ) -> Sequence[BaseTool[AgentDependencies, Any, Any]]:
        del context
        return self._tools


class RecordingHook(BaseToolHook[AgentDependencies]):
    async def before_tool(
        self,
        context: ToolExecutionContext[AgentDependencies],
        tool_id: str,
        arguments: BaseModel,
    ) -> None:
        del arguments
        context.run.deps.events.append(f'before:{tool_id}')

    async def after_tool(
        self,
        context: ToolExecutionContext[AgentDependencies],
        tool_id: str,
        result: ToolResult,
    ) -> None:
        del result
        context.run.deps.events.append(f'after:{tool_id}')


def structured_definition(
    *,
    model: ModelRef | ModelRouteRef,
    tool: AddTool,
    hook: RecordingHook,
    policy: AgentRunPolicy = AgentRunPolicy(),
) -> AgentDefinition[AgentDependencies, StructuredAnswer]:
    capability = BaseCapability[AgentDependencies](
        id='arithmetic',
        contributions=CapabilityContributions(
            instructions=('Use the add tool before answering.',),
            tools=(tool,),
        ),
    )

    return AgentDefinition(
        model=model,
        deps_type=AgentDependencies,
        output_type=StructuredAnswer,
        instructions=('Return a structured answer.',),
        capabilities=(capability,),
        hooks=(hook,),
        policy=policy,
    )


def waiting_definition(
    *,
    tool: WaitTool,
    policy: AgentRunPolicy = AgentRunPolicy(),
) -> AgentDefinition[AgentDependencies, str]:
    return AgentDefinition(
        model=ModelRef(name='primary'),
        deps_type=AgentDependencies,
        output_type=str,
        toolsets=(ConsumerToolset((tool,)),),
        policy=policy,
    )


def text_definition() -> AgentDefinition[AgentDependencies, str]:
    capability = BaseCapability[AgentDependencies](
        id='writing',
        contributions=CapabilityContributions(instructions=('Write plain text.',)),
    )

    return AgentDefinition(
        model=ModelRef(name='primary'),
        deps_type=AgentDependencies,
        output_type=str,
        capabilities=(capability,),
    )
