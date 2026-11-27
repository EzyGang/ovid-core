import asyncio
from collections.abc import AsyncIterator

import pytest
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls

from ovid_core import AgentRunPolicy, ProviderError
from ovid_core.errors import AgentTimeoutError
from tests.support.agent_consumer import AgentDependencies
from tests.support.deferred_consumer import SafeTool
from tests.support.partial_consumer import registration, shape


_FAILED = 'Provider request failed (unavailable)'


async def test_stream_partial_drops_unparsable_calls_of_interrupted_response() -> None:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        del messages, info
        yield {0: DeltaToolCall(name='safe', json_args='{"value": 1}', tool_call_id='c1')}
        yield {1: DeltaToolCall(name='safe', json_args='{"val', tool_call_id='c2')}
        raise ModelAPIError('partial', 'provider down')

    agent = (await registration(stream, (SafeTool(),))).agent

    with pytest.raises(ProviderError, match='Provider request failed'):
        async with agent.stream('Go.', deps=AgentDependencies(prefix='partial')) as events:
            async for _ in events:
                pass

    partial = events.partial()
    assert partial is not None
    assert (partial.outcome, partial.error_type, partial.message) == ('failed', 'ProviderError', _FAILED)
    assert shape(partial.messages) == [
        ('request', 'complete', ('user_prompt:Go.',)),
        ('response', 'interrupted', ('call:c1',)),
        ('response', 'interrupted', (f'text:[Run ended: failed: {_FAILED}]',)),
    ]
    assert partial.history == partial.messages
    assert partial.usage.request_count == 1


async def test_stream_partial_records_policy_timeout_and_completed_stream_has_none() -> None:
    calls: list[str] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del messages, info
        calls.append('model')
        yield 'Hello'
        if len(calls) == 1:
            await asyncio.sleep(10)

    slow = (await registration(stream, policy=AgentRunPolicy(timeout_seconds=0.1))).agent
    deps = AgentDependencies(prefix='partial')

    with pytest.raises(AgentTimeoutError):
        async with slow.stream('Wait.', deps=deps) as events:
            async for _ in events:
                pass
    async with slow.stream('Again.', deps=deps) as completed:
        async for _ in completed:
            pass

    partial = events.partial()
    assert partial is not None
    assert (partial.outcome, partial.message) == ('timeout', 'Agent run timed out')
    assert shape(partial.messages)[-2:] == [
        ('response', 'interrupted', ('text:Hello',)),
        ('response', 'interrupted', ('text:[Run ended: timeout: Agent run timed out]',)),
    ]
    assert completed.partial() is None
