from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
from pydantic_ai import Agent, InstrumentationSettings
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pytest_mock import MockerFixture

import tests.support.agent_consumer as consumer
from ovid_core import AgentRunError, AgentRunPolicy, ObservabilityConfig
from ovid_core.routing import ModelRef, ModelRouteRef
from tests.support.agent_consumer import AddTool, AgentDependencies, RecordingHook
from tests.support.agent_helpers import agent_factory, failing_request, structured_test_model
from tests.support.helpers import CONVERSATION_ID, RUN_ID


async def text_stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
    del messages, info
    yield 'Hello'
    yield ' world'


@pytest.mark.asyncio
async def test_factory_compiles_exact_inputs_fallback_diagnostics_and_continuation() -> None:
    failing = FunctionModel(failing_request, model_name='failing')
    working = structured_test_model()
    factory = agent_factory({'failing': failing, 'working': working}, route=True)
    tool = AddTool()
    hook = RecordingHook()
    agent = await factory.build(
        consumer.structured_definition(
            model=ModelRouteRef(name='answer'),
            tool=tool,
            hook=hook,
            policy=AgentRunPolicy(max_concurrency=1),
            observability=ObservabilityConfig(enabled=True),
        )
    )
    deps = AgentDependencies(prefix='consumer')

    result = await agent.run(
        'Add values.',
        deps=deps,
        run_id=RUN_ID,
        conversation_id=CONVERSATION_ID,
    )

    assert result.output.value == 'done'
    assert result.run_id == RUN_ID
    assert result.conversation_id == CONVERSATION_ID
    assert result.usage.request_count == 2
    assert deps.events == ['before:add', 'consumer:add', 'after:add']
    assert agent.diagnostics.provider == 'test'
    assert agent.diagnostics.model == 'failing'
    assert agent.diagnostics.selected_model == 'failing'
    assert agent.diagnostics.fallback_order == ('failing', 'working')
    assert [item.kind for item in agent.diagnostics.extensions] == ['instructions', 'capability', 'tool', 'hook']
    assert 'provider-secret' not in agent.diagnostics.model_dump_json()

    parameters = working.last_model_request_parameters
    assert parameters is not None
    assert [tool.name for tool in parameters.function_tools] == ['add']
    request_instructions = tuple(
        message.instructions for message in result.messages if message.instructions is not None
    )
    assert request_instructions == (
        'Return a structured answer.\nUse the add tool before answering.',
        'Return a structured answer.\nUse the add tool before answering.',
    )

    continued = await agent.run('Continue.', deps=deps, messages=result.messages)
    assert continued.conversation_id == result.conversation_id
    assert continued.run_id != result.run_id


@pytest.mark.asyncio
async def test_instrumentation_preserves_global_defaults_and_supports_per_agent_settings(
    mocker: MockerFixture,
) -> None:
    factory = agent_factory({'primary': structured_test_model()})
    definition = consumer.text_definition()
    instrument = mocker.patch.object(Agent, 'instrument', new_callable=mocker.PropertyMock)

    await factory.build(definition)
    instrument.assert_not_called()

    await factory.build(replace(definition, observability=ObservabilityConfig(enabled=True, include_content=True)))
    settings = instrument.call_args.args[0]

    assert isinstance(settings, InstrumentationSettings)
    assert settings.include_content is True


@pytest.mark.asyncio
async def test_stream_normalizes_ordered_lifecycle_tools_output_and_usage() -> None:
    factory = agent_factory({'working': structured_test_model()})
    agent = await factory.build(
        consumer.structured_definition(model=ModelRef(name='working'), tool=AddTool(), hook=RecordingHook())
    )

    async with agent.stream(
        'Add values.',
        deps=AgentDependencies(prefix='stream'),
        run_id=RUN_ID,
        conversation_id=CONVERSATION_ID,
    ) as stream:
        with pytest.raises(AgentRunError, match='has not completed'):
            _ = stream.result
        events = [event async for event in stream]
        result = stream.result

    assert result.output.value == 'done'
    assert result.usage == events[-1].usage
    assert [event.sequence for event in events] == list(range(len(events)))
    assert [event.kind for event in events] == [
        'run_started',
        'model_request_started',
        'tool_call',
        'tool_result',
        'model_request_started',
        'usage_update',
        'run_completed',
    ]
    assert all(event.run_id == RUN_ID for event in events)
    assert all(event.conversation_id == CONVERSATION_ID for event in events)
    assert any(part.kind == 'tool_call' for message in result.messages for part in message.parts)
    assert any(part.kind == 'tool_return' for message in result.messages for part in message.parts)


@pytest.mark.asyncio
async def test_text_stream_preserves_deltas_without_implicit_tools() -> None:
    model = FunctionModel(stream_function=text_stream, model_name='text')
    factory = agent_factory({'primary': model})
    agent = await factory.build(consumer.text_definition())

    async with agent.stream('Write.', deps=AgentDependencies(prefix='text')) as stream:
        events = [event async for event in stream]

    assert ''.join(event.content for event in events if event.kind == 'text_delta') == 'Hello world'
    assert [event.kind for event in events] == [
        'run_started',
        'model_request_started',
        'text_delta',
        'text_delta',
        'usage_update',
        'run_completed',
    ]
