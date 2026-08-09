import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from pydantic import ValidationError
from pydantic_ai.messages import AgentStreamEvent, PartStartEvent
from pydantic_ai.messages import ToolCallPart as PydanticToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

import tests.agent_consumer as consumer
from ovid_core.adapters.pydantic_ai._agent_errors import normalize_run_error
from ovid_core.adapters.pydantic_ai._agent_stream import PydanticAIStream
from ovid_core.agents import AgentDefinition, AgentRunPolicy, AgentStream, AgentUsageLimits, OvidAgent
from ovid_core.capabilities.base import BaseCapability, CapabilityContributions
from ovid_core.errors import AgentConstructionError, AgentRunError, AgentTimeoutError, ExtensionCollisionError
from ovid_core.routing.models import ModelRef
from ovid_core.runtime.events import AgentEvent
from tests.agent_consumer import AddTool, AgentDependencies, ConsumerToolset, RecordingHook, WaitTool
from tests.agent_helpers import UnsupportedRuntime, agent_factory, failing_request, structured_test_model
from tests.helpers import CONVERSATION_ID, RUN_ID


@pytest.mark.asyncio
async def test_run_cancellation_timeout_limits_and_failures_are_stable() -> None:
    wait_tool = WaitTool()
    wait_factory = agent_factory({'primary': TestModel(call_tools=['wait'])})
    waiting_agent = await wait_factory.build(consumer.waiting_definition(tool=wait_tool))
    deps = AgentDependencies(prefix='cancel')
    task = asyncio.create_task(waiting_agent.run('Wait.', deps=deps))
    await deps.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wait_tool.cancelled is True

    timeout_tool = WaitTool()
    timeout_agent = await wait_factory.build(
        consumer.waiting_definition(tool=timeout_tool, policy=AgentRunPolicy(timeout_seconds=0.1))
    )
    timeout_deps = AgentDependencies(prefix='timeout')
    timeout_task = asyncio.create_task(timeout_agent.run('Wait.', deps=timeout_deps))
    await timeout_deps.started.wait()
    with pytest.raises(AgentTimeoutError, match='timed out'):
        await timeout_task
    assert timeout_tool.cancelled is True

    limited_factory = agent_factory({'primary': structured_test_model()})
    limited_agent = await limited_factory.build(
        consumer.structured_definition(
            model=ModelRef(name='primary'),
            tool=AddTool(),
            hook=RecordingHook(),
            policy=AgentRunPolicy(limits=AgentUsageLimits(requests=1)),
        )
    )
    with pytest.raises(AgentRunError, match='Agent run failed'):
        await limited_agent.run('Add.', deps=AgentDependencies(prefix='limited'))

    failing_factory = agent_factory({'primary': FunctionModel(failing_request, model_name='failing')})
    failing_agent = await failing_factory.build(consumer.waiting_definition(tool=WaitTool()))
    with pytest.raises(AgentRunError, match='Agent run failed') as error:
        await failing_agent.run('Fail.', deps=AgentDependencies(prefix='failure'))
    assert 'provider-secret' not in str(error.value)


@pytest.mark.asyncio
async def test_stream_cancellation_timeout_and_failure_cleanup_are_stable() -> None:
    wait_factory = agent_factory({'primary': TestModel(call_tools=['wait'])})
    cancelled_tool = WaitTool()
    cancelled_agent = await wait_factory.build(consumer.waiting_definition(tool=cancelled_tool))
    cancelled_deps = AgentDependencies(prefix='stream-cancel')

    owner = asyncio.create_task(_consume_agent_stream(cancelled_agent, cancelled_deps))
    await cancelled_deps.started.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert cancelled_tool.cancelled is True

    timeout_tool = WaitTool()
    timeout_agent = await wait_factory.build(
        consumer.waiting_definition(tool=timeout_tool, policy=AgentRunPolicy(timeout_seconds=0.1))
    )
    with pytest.raises(AgentTimeoutError, match='timed out'):
        async with timeout_agent.stream('Wait.', deps=AgentDependencies(prefix='stream-timeout')) as stream:
            await _collect(stream)
    assert timeout_tool.cancelled is True

    failing_factory = agent_factory({'primary': FunctionModel(failing_request, model_name='failing')})
    failing_agent = await failing_factory.build(consumer.waiting_definition(tool=WaitTool()))
    async with failing_agent.stream('Fail.', deps=AgentDependencies(prefix='failure')) as stream:
        assert (await anext(stream)).kind == 'run_started'
        failed = await anext(stream)
        assert failed.kind == 'run_failed'
        assert failed.error_type == 'AgentRunError'
        assert failed.message == 'Agent run failed'
        with pytest.raises(AgentRunError, match='Agent run failed'):
            await anext(stream)


@pytest.mark.asyncio
async def test_construction_failures_extension_provenance_and_core_only_surface() -> None:
    unsupported_factory = agent_factory({'primary': UnsupportedRuntime()})
    with pytest.raises(AgentConstructionError, match='not compatible'):
        await unsupported_factory.build(consumer.waiting_definition(tool=WaitTool()))

    factory = agent_factory({'primary': TestModel(custom_output_text='done')})
    local_hook = RecordingHook()
    direct_hook = RecordingHook()
    capability = BaseCapability[AgentDependencies](
        id='empty-tools',
        contributions=CapabilityContributions(
            toolsets=(ConsumerToolset(()),),
            hooks=(local_hook,),
        ),
    )
    definition = AgentDefinition(
        model=ModelRef(name='primary'),
        deps_type=AgentDependencies,
        output_type=str,
        capabilities=(capability,),
        hooks=(direct_hook,),
    )
    agent = await factory.build(definition)
    assert [item.kind for item in agent.diagnostics.extensions] == ['capability', 'toolset', 'hook', 'hook']

    duplicate_capabilities = AgentDefinition(
        model=ModelRef(name='primary'),
        deps_type=AgentDependencies,
        output_type=str,
        capabilities=(BaseCapability(id='same'), BaseCapability(id='same')),
    )
    with pytest.raises(ExtensionCollisionError, match="Duplicate capability ID: 'same'"):
        await factory.build(duplicate_capabilities)

    invalid_definition = AgentDefinition(
        model=ModelRef(name='primary'),
        deps_type=AgentDependencies,
        output_type=cast(type[str], cast(Any, 42)),
    )
    with pytest.raises(AgentConstructionError, match='construction failed'):
        await factory.build(invalid_definition)

    public_values = (value for name, value in vars(consumer).items() if not name.startswith('_'))
    assert all(not getattr(value, '__module__', '').startswith('pydantic_ai') for value in public_values)


def test_error_normalization_preserves_owned_errors_and_rejects_invalid_data() -> None:
    owned = AgentRunError('safe')
    assert normalize_run_error(owned) is owned

    with pytest.raises(ValidationError) as invalid:
        AgentUsageLimits(requests=0)
    assert str(normalize_run_error(invalid.value)) == 'Pydantic AI returned invalid agent data'


async def _multiple_response_parts() -> AsyncIterator[AgentStreamEvent]:
    yield PartStartEvent(index=0, part=PydanticToolCallPart('first', {}, 'call-1'))
    yield PartStartEvent(index=1, part=PydanticToolCallPart('second', {}, 'call-2'))


@pytest.mark.asyncio
async def test_multiple_response_parts_share_one_model_request_event() -> None:
    stream = PydanticAIStream[str](
        events=_multiple_response_parts(),
        run_id=RUN_ID,
        conversation_id=CONVERSATION_ID,
    )

    assert [event.kind for event in await _collect(stream)] == ['run_started', 'model_request_started']


async def _collect(stream: AgentStream[str]) -> list[AgentEvent]:
    return [event async for event in stream]


async def _consume_agent_stream(
    agent: OvidAgent[AgentDependencies, str],
    deps: AgentDependencies,
) -> None:
    async with agent.stream('Wait.', deps=deps) as stream:
        await _collect(stream)
