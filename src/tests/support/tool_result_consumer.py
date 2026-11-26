from collections.abc import AsyncIterator
from dataclasses import dataclass

from pydantic_ai.messages import ModelMessage, ModelRequest
from pydantic_ai.messages import ToolReturnPart as PydanticToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.profiles import ModelProfile

from ovid_core import BaseModel
from ovid_core.agents import AgentDefinition
from ovid_core.capabilities import BaseCapability, CapabilityContributions
from ovid_core.messages import ToolReturnPart
from ovid_core.routing import ModelRef
from ovid_core.runtime import AgentEvent, RunResult, ToolResultEvent
from ovid_core.tools import BaseTool, ToolExecutionContext, ToolResult
from tests.support.agent_helpers import agent_factory


class NoArguments(BaseModel):
    pass


class FixedResultTool(BaseTool[None, NoArguments, ToolResult]):
    id = 'report'
    description = 'Return a fixed result.'
    args_type = NoArguments
    result_type = ToolResult

    def __init__(self, result: ToolResult) -> None:
        self.result = result

    async def execute(self, context: ToolExecutionContext[None], arguments: NoArguments) -> ToolResult:
        del context, arguments
        return self.result


@dataclass(frozen=True, slots=True)
class ToolRun:
    """One run in which the model calls `report` once and then answers."""

    requests: tuple[tuple[ModelMessage, ...], ...]
    result: RunResult[str]
    events: tuple[AgentEvent, ...]

    @property
    def sent_return(self) -> PydanticToolReturnPart:
        """The tool return part that the model received in its second request."""
        request = self.requests[1][-1]
        assert isinstance(request, ModelRequest)
        part = request.parts[0]
        assert isinstance(part, PydanticToolReturnPart)

        return part

    @property
    def stored_return(self) -> ToolReturnPart:
        part = self.result.messages[2].parts[0]
        assert isinstance(part, ToolReturnPart)

        return part

    @property
    def result_event(self) -> ToolResultEvent:
        (event,) = (event for event in self.events if isinstance(event, ToolResultEvent))

        return event


async def run_tool(result: ToolResult, *, profile: ModelProfile | None = None) -> ToolRun:
    requests: list[tuple[ModelMessage, ...]] = []

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        del info
        requests.append(tuple(messages))
        if len(messages) > 1:
            yield 'Done.'
            return

        yield {0: DeltaToolCall(name='report', json_args='{}', tool_call_id='call-1')}

    capability = BaseCapability[None](
        id='report',
        contributions=CapabilityContributions(tools=(FixedResultTool(result),)),
    )
    definition = AgentDefinition[None, str](
        model=ModelRef(name='primary'),
        deps_type=type(None),
        output_type=str,
        capabilities=(capability,),
    )
    model = FunctionModel(stream_function=respond, profile=profile)
    agent = await agent_factory({'primary': model}).build(definition)

    async with agent.stream('Report.', deps=None) as stream:
        events = tuple([event async for event in stream])
    outcome = stream.result
    assert isinstance(outcome, RunResult)

    return ToolRun(requests=tuple(requests), result=outcome, events=events)
