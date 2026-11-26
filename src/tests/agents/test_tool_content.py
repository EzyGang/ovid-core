from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import JsonValue
from pydantic_ai.messages import (
    SYNTHESIZED_TOOL_RETURN_METADATA_KEY,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.messages import ToolReturnPart as PydanticToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ovid_core import MessageCodec
from ovid_core.adapters.pydantic_ai import message_from_pydantic, message_to_pydantic
from ovid_core.agents import AgentDefinition
from ovid_core.messages import ToolReturnPart
from ovid_core.routing import ModelRef
from ovid_core.runtime import DeferredRunResult, RunResult, ToolResultEvent
from ovid_core.tools import ToolCallApproved, ToolExecutionContext, ToolResult
from tests.support.agent_consumer import AgentDependencies
from tests.support.agent_helpers import agent_factory
from tests.support.deferred_consumer import DangerTool, ScriptedModel, ValueArguments, deferred_agent
from tests.support.helpers import CONVERSATION_ID, RUN_ID
from tests.support.tool_result_consumer import run_tool


class AuditedDangerTool(DangerTool):
    async def execute(
        self,
        context: ToolExecutionContext[AgentDependencies],
        arguments: ValueArguments,
    ) -> ToolResult:
        result = await super().execute(context, arguments)

        return result.model_copy(update={'metadata': {'audit': arguments.value}})


_MCP_APP_METADATA = {
    'mcp_app': {
        'resource_uri': 'ui://dashboard',
        'tool_input': {'city': 'Oslo'},
        'result': {'content': [{'type': 'text', 'text': 'Sunny'}], 'isError': False},
        'toolset_id': 'weather',
    }
}


@pytest.mark.parametrize(
    ('content', 'metadata', 'model_text', 'stored_metadata'),
    [
        ('a\nb', {'k': 1}, 'a\nb', {'k': 1}),
        ({'rows': [1, 2], 'note': 'x'}, {}, '{"rows":[1,2],"note":"x"}', None),
    ],
)
async def test_model_receives_only_tool_content_and_metadata_is_stored_and_streamed(
    content: JsonValue,
    metadata: dict[str, JsonValue],
    model_text: str,
    stored_metadata: JsonValue,
) -> None:
    run = await run_tool(ToolResult(content=content, metadata=metadata))

    stored_message = run.result.messages[2]
    restored = message_to_pydantic(stored_message)

    assert run.sent_return.content == content
    assert run.sent_return.model_response_str() == model_text
    assert run.sent_return.metadata == stored_metadata
    assert run.stored_return == ToolReturnPart(
        tool_name='report',
        content=content,
        tool_call_id='call-1',
        metadata=stored_metadata,
    )
    assert run.result_event.content == content
    assert run.result_event.metadata == stored_metadata
    assert isinstance(restored, ModelRequest)
    assert isinstance(restored.parts[0], PydanticToolReturnPart)
    assert restored.parts[0].metadata == stored_metadata
    assert MessageCodec().decode(MessageCodec().encode(stored_message)) == stored_message


@pytest.mark.parametrize(
    'metadata',
    [
        None,
        {'k': 1},
        {SYNTHESIZED_TOOL_RETURN_METADATA_KEY: True},
        _MCP_APP_METADATA,
        ['first', 2],
        'note',
        7,
        True,
    ],
)
async def test_every_upstream_tool_return_metadata_value_round_trips_and_continues_a_run(metadata: JsonValue) -> None:
    requests: list[list[ModelMessage]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        requests.append(messages)

        return ModelResponse(parts=(TextPart('Continued.'),))

    codec = MessageCodec()
    upstream = (
        ModelRequest(parts=[PydanticToolReturnPart('report', 'done', 'call-1', metadata=metadata)]),
        ModelResponse(parts=[TextPart('Reported.')]),
    )
    history = (
        message_from_pydantic(ModelResponse(parts=[ToolCallPart('report', {}, 'call-1')])),
        *(message_from_pydantic(message) for message in upstream),
    )
    history = tuple(
        codec.decode(codec.encode(message.model_copy(update={'run_id': RUN_ID, 'conversation_id': CONVERSATION_ID})))
        for message in history
    )
    definition = AgentDefinition[None, str](model=ModelRef(name='primary'), deps_type=type(None), output_type=str)
    agent = await agent_factory({'primary': FunctionModel(respond)}).build(definition)

    result = await agent.run('Continue.', deps=None, messages=history)

    sent = requests[0][1]
    assert result.output == 'Continued.'
    assert isinstance(sent, ModelRequest)
    assert isinstance(sent.parts[0], PydanticToolReturnPart)
    assert sent.parts[0].metadata == metadata
    assert type(sent.parts[0].metadata) is type(metadata)


def test_metadata_without_a_json_form_is_stored_as_its_json_form() -> None:
    metadata: dict[str, Any] = {'at': datetime(2026, 1, 2, tzinfo=UTC), 'raw': b'\xfb\xff', 'handle': object()}
    request = ModelRequest(parts=[PydanticToolReturnPart('report', 'done', 'call-1', metadata=metadata)])

    (part,) = message_from_pydantic(request).parts

    assert isinstance(part, ToolReturnPart)
    assert part.metadata == {'at': '2026-01-02T00:00:00Z', 'raw': '-_8=', 'handle': None}


def test_tool_result_event_without_metadata_decodes_for_older_senders() -> None:
    event = ToolResultEvent.model_validate(
        {
            'kind': 'tool_result',
            'run_id': str(RUN_ID),
            'conversation_id': str(CONVERSATION_ID),
            'sequence': 3,
            'tool_name': 'report',
            'content': 'done',
            'tool_call_id': 'call-1',
        }
    )

    assert event.metadata is None


async def test_approved_deferred_call_keeps_the_tool_metadata_out_of_band() -> None:
    agent = await deferred_agent(ScriptedModel(('danger', {'value': 1}, 'c1')), (AuditedDangerTool(),))
    deps = AgentDependencies(prefix='deferred')
    paused = await agent.run('Delete.', deps=deps)
    assert isinstance(paused, DeferredRunResult)

    approved = await agent.run(
        resume=paused.requests.resume({'c1': ToolCallApproved()}),
        deps=deps,
        messages=paused.history,
    )

    returns = [part for message in approved.messages for part in message.parts if isinstance(part, ToolReturnPart)]
    assert isinstance(approved, RunResult)
    assert returns == [ToolReturnPart(tool_name='danger', content='danger 1', tool_call_id='c1', metadata={'audit': 1})]
