from collections.abc import AsyncIterator

from pydantic_ai.capabilities import Toolset
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.messages import TextPart as PydanticTextPart
from pydantic_ai.messages import ToolCallPart as PydanticToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from ovid_core.adapters.pydantic_ai import pydantic_ai_capability
from ovid_core.agents import AgentDefinition
from ovid_core.messages import AgentMessage, OpaqueResponsePart, ToolCallPart
from ovid_core.routing import ModelRef
from ovid_core.runtime import RunCompletedEvent, ToolCallEvent, ToolResultEvent
from tests.support.agent_helpers import agent_factory


def _response_parts(messages: tuple[AgentMessage, ...]) -> list[tuple[type, str]]:
    return [
        (type(part), part.part_kind if isinstance(part, OpaqueResponsePart) else part.kind)
        for message in messages
        if message.role == 'response'
        for part in message.parts[:1]
    ]


async def test_tool_call_without_a_name_does_not_fail_a_run_or_a_stream() -> None:
    async def lookup() -> str:
        """Look up a value."""
        return 'found'

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        if len(messages) == 1:
            return ModelResponse(parts=(PydanticToolCallPart('', {}, 'unnamed'),))
        if len(messages) == 3:
            return ModelResponse(parts=(PydanticToolCallPart('lookup', {}, 'call-1'),))

        return ModelResponse(parts=(PydanticTextPart('Done.'),))

    async def respond_stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        del info
        if len(messages) == 1:
            yield {0: DeltaToolCall(name='', json_args='{}', tool_call_id='unnamed')}
        elif len(messages) == 3:
            yield {0: DeltaToolCall(name='lookup', json_args='{}', tool_call_id='call-1')}
        else:
            yield 'Done.'

    definition = AgentDefinition[None, str](
        model=ModelRef(name='primary'),
        deps_type=type(None),
        output_type=str,
        capabilities=(pydantic_ai_capability(Toolset(FunctionToolset([lookup]), id='lookups')),),
    )
    model = FunctionModel(respond, stream_function=respond_stream)
    agent = await agent_factory({'primary': model}).build(definition)

    result = await agent.run('Look.', deps=None)
    async with agent.stream('Look.', deps=None) as stream:
        events = [event async for event in stream]

    tool_events = [
        (event.kind, event.tool_name, event.tool_call_id)
        for event in events
        if isinstance(event, ToolCallEvent | ToolResultEvent)
    ]
    expected_parts = [(OpaqueResponsePart, 'tool-call'), (ToolCallPart, 'tool_call')]

    assert result.output == stream.result.output == 'Done.'
    assert tool_events == [('tool_call', 'lookup', 'call-1'), ('tool_result', 'lookup', 'call-1')]
    assert isinstance(events[-1], RunCompletedEvent)
    assert [event.request_index for event in events if event.kind == 'model_request_started'] == [0, 1, 2]
    assert _response_parts(result.history)[:2] == expected_parts
    assert _response_parts(stream.result.history)[:2] == expected_parts
