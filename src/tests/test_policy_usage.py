import asyncio

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

import tests.policy_consumer as consumer
from ovid_core.agents import AgentDefinition
from ovid_core.errors import UsageLimitError
from ovid_core.policy import AgentRetryPolicy, AgentRunPolicy, AgentUsageLimits
from ovid_core.routing.models import ModelRef, ModelRouteRef
from ovid_core.usage.models import Usage
from ovid_core.usage.tracking import UsageTracker
from tests.agent_helpers import agent_factory, failing_request


class WaitingRequest:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        self.started.set()
        await self.release.wait()
        raise AssertionError('waiting request unexpectedly released')


class RetryingOutput:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        self.calls += 1
        arguments = {} if self.calls == 1 else {'prompt': 'done'}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, arguments)])


@pytest.mark.asyncio
async def test_subagent_usage_is_aggregated_once() -> None:
    factory = agent_factory(
        {
            'child': TestModel(custom_output_text='child', model_name='child'),
            'parent': TestModel(call_tools=['delegate'], custom_output_text='parent', model_name='parent'),
        }
    )
    child = await factory.build(consumer.child_definition())
    updates: list[Usage] = []

    async def on_update(value: UsageTracker, delta: Usage) -> None:
        del delta
        updates.append(value.usage)

    tracker = UsageTracker(on_update=on_update)
    tool = consumer.DelegateTool(child=child, tracker=tracker)
    parent = await factory.build(consumer.parent_definition(tool))
    result = await parent.run('parent-secret-prompt', deps=None, usage_tracker=tracker)

    assert tool.child_result is not None
    assert tracker.usage == result.usage + tool.child_result.usage
    assert tracker.usage.request_count == 3
    assert updates[-1] == tracker.usage


@pytest.mark.asyncio
async def test_aggregate_request_limit_stops_parent_after_subagent() -> None:
    factory = agent_factory(
        {
            'child': TestModel(custom_output_text='child', model_name='child'),
            'parent': TestModel(call_tools=['delegate'], custom_output_text='parent', model_name='parent'),
        }
    )
    child = await factory.build(consumer.child_definition())
    tracker = UsageTracker(limits=AgentUsageLimits(requests=2))
    parent = await factory.build(consumer.parent_definition(consumer.DelegateTool(child=child, tracker=tracker)))

    with pytest.raises(UsageLimitError, match='Aggregate request limit reached'):
        await parent.run('delegate', deps=None, usage_tracker=tracker)

    assert tracker.usage.request_count == 2
    assert tracker.usage.tool_calls == 1


@pytest.mark.asyncio
async def test_tracker_covers_fallback_and_stream_without_double_counting() -> None:
    fallback_factory = agent_factory(
        {
            'failing': FunctionModel(failing_request, model_name='failing'),
            'working': TestModel(custom_output_text='working', model_name='working'),
        },
        route=True,
    )
    fallback = await fallback_factory.build(
        AgentDefinition(
            model=ModelRouteRef(name='answer'),
            deps_type=type(None),
            output_type=str,
        )
    )
    tracker = UsageTracker()
    result = await fallback.run('fallback', deps=None, usage_tracker=tracker)
    assert tracker.usage == result.usage
    stream_agent = await agent_factory(
        {'working': TestModel(custom_output_text='working', model_name='working')}
    ).build(AgentDefinition(model=ModelRef(name='working'), deps_type=type(None), output_type=str))

    stream_tracker = UsageTracker()
    async with stream_agent.stream('stream', deps=None, usage_tracker=stream_tracker) as stream:
        events = [event async for event in stream]
    assert events[-1].kind == 'run_completed'
    assert stream_tracker.usage == stream.result.usage


@pytest.mark.asyncio
async def test_cancelled_run_preserves_partial_tracked_usage() -> None:
    request = WaitingRequest()
    factory = agent_factory({'waiting': FunctionModel(request.__call__, model_name='waiting')})
    agent = await factory.build(AgentDefinition(model=ModelRef(name='waiting'), deps_type=type(None), output_type=str))
    tracker = UsageTracker()
    task = asyncio.create_task(agent.run('cancel', deps=None, usage_tracker=tracker))
    await request.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tracker.usage == Usage()


@pytest.mark.asyncio
async def test_output_retry_usage_is_aggregated_once() -> None:
    output = RetryingOutput()
    factory = agent_factory({'retrying': FunctionModel(output.__call__, model_name='retrying')})
    agent = await factory.build(
        AgentDefinition(
            model=ModelRef(name='retrying'),
            deps_type=type(None),
            output_type=consumer.DelegateArgs,
            policy=AgentRunPolicy(retries=AgentRetryPolicy(output=1)),
        )
    )
    tracker = UsageTracker()
    result = await agent.run('retry', deps=None, usage_tracker=tracker)

    assert output.calls == 2
    assert result.usage.request_count == 2
    assert tracker.usage == result.usage


def test_subagent_consumer_has_no_adapter_dependency() -> None:
    public_values = (value for name, value in vars(consumer).items() if not name.startswith('_'))
    assert all(not getattr(value, '__module__', '').startswith('pydantic_ai') for value in public_values)
