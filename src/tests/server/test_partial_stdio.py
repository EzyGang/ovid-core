from collections.abc import AsyncIterator, Callable

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCalls

from ovid_core import AgentRetryPolicy, AgentRunPolicy, AgentUsageLimits, InMemoryConversationStore
from ovid_core.persistence import ConversationStore
from ovid_core.runtime import ConversationId
from ovid_core.server import AgentRunRequest, StdioCancelRequest, StdioRunRequest
from ovid_core.tools import ToolExecutionContext
from tests.support.agent_consumer import AgentDependencies
from tests.support.deferred_consumer import RecordingTool, SafeTool
from tests.support.partial_consumer import (
    AppendOnlyStore,
    WaitingTool,
    boundary,
    failure,
    registration,
    returned_calls,
    shape,
    stdio_session,
    tool_call,
    unanswered_calls,
)


_CONVERSATION_ID = ConversationId.new()
_RAISE = AgentRunPolicy(retries=AgentRetryPolicy(tool_failures='raise'))


class BrokenTool(RecordingTool):
    id = 'broken'

    def defer(self, context: ToolExecutionContext[AgentDependencies]) -> None:
        del context
        raise RuntimeError('private tool detail')


def _run(request_id: str, prompt: str) -> StdioRunRequest:
    return StdioRunRequest(
        request_id=request_id,
        agent_id='writer',
        request=AgentRunRequest(prompt=prompt, conversation_id=_CONVERSATION_ID),
    )


@pytest.mark.parametrize('store_type', [InMemoryConversationStore, AppendOnlyStore])
async def test_stdio_persists_partial_when_tool_raises(store_type: Callable[[], ConversationStore]) -> None:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        del messages, info
        yield tool_call('broken', 'c1')

    store = store_type()
    agent = await registration(stream, (BrokenTool(),), policy=_RAISE)

    frames = await stdio_session(agent, store, _run('r1', 'Break.'), 'r1')

    stored = await store.load(_CONVERSATION_ID)
    assert shape(stored) == [
        ('request', 'complete', ('user_prompt:Break.',)),
        ('response', 'complete', ('call:c1',)),
        ('request', 'interrupted', ()),
        boundary("failed: Tool 'broken' failed"),
    ]
    assert stored[-1].finish_reason == 'error'
    assert 'private' not in str(stored)
    assert failure(frames, 'r1') == ([True], 'server_failure')


async def test_stdio_persists_every_response_and_return_when_request_limit_is_reached() -> None:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        del info
        yield tool_call('safe', f'c{returned_calls(messages)}')

    store = InMemoryConversationStore()
    policy = AgentRunPolicy(limits=AgentUsageLimits(requests=50))
    agent = await registration(stream, (SafeTool(),), policy=policy)

    frames = await stdio_session(agent, store, _run('r1', 'Loop.'), 'r1')

    stored = shape(await store.load(_CONVERSATION_ID))
    assert len(stored) == 102
    assert stored[0] == ('request', 'complete', ('user_prompt:Loop.',))
    assert stored[1:-1:2] == [('response', 'complete', (f'call:c{index}',)) for index in range(50)]
    assert stored[2:-1:2] == [('request', 'complete', (f'return:c{index}:success',)) for index in range(50)]
    assert stored[-1] == boundary('usage_limit: Agent usage limit exceeded')
    assert failure(frames, 'r1') == ([True], 'agent_run_failed')


async def test_stdio_persists_partial_on_context_overflow() -> None:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        del info
        if returned_calls(messages):
            raise ModelHTTPError(400, 'partial', body={'error': {'code': 'context_length_exceeded'}})
        yield tool_call('safe', 'c1')

    store = InMemoryConversationStore()
    agent = await registration(stream, (SafeTool(),))

    frames = await stdio_session(agent, store, _run('r1', 'Read.'), 'r1')

    assert shape(await store.load(_CONVERSATION_ID)) == [
        ('request', 'complete', ('user_prompt:Read.',)),
        ('response', 'complete', ('call:c1',)),
        ('request', 'complete', ('return:c1:success',)),
        boundary('context_overflow: Model context window is full. Compact the conversation or start a new one'),
    ]
    assert failure(frames, 'r1') == ([True], 'context_window_exceeded')


async def test_stdio_cancel_during_tool_persists_interrupted_request() -> None:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        del messages, info
        yield tool_call('wait', 'c1')

    store = InMemoryConversationStore()
    tool = WaitingTool()
    agent = await registration(stream, (tool,))
    cancel = StdioCancelRequest(request_id='cancel', target_request_id='r1')

    frames = await stdio_session(agent, store, _run('r1', 'Wait.'), tool.started, cancel, 'r1')

    assert shape(await store.load(_CONVERSATION_ID)) == [
        ('request', 'complete', ('user_prompt:Wait.',)),
        ('response', 'complete', ('call:c1',)),
        ('request', 'interrupted', ()),
        boundary('cancelled: Run cancelled'),
    ]
    assert failure(frames, 'r1') == ([True], 'run_cancelled')


async def test_stdio_eof_during_tool_persists_partial_and_next_run_closes_the_call() -> None:
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        del info
        requests.append(messages)
        if len(requests) == 1:
            yield tool_call('wait', 'c1')
            return
        yield 'done'

    store = InMemoryConversationStore()
    tool = WaitingTool()
    agent = await registration(stream, (tool,))

    interrupted = await stdio_session(agent, store, _run('r1', 'Wait.'), tool.started)
    partial = await store.load(_CONVERSATION_ID)
    following = await stdio_session(agent, store, _run('r2', 'Again.'), 'r2')

    assert shape(partial) == [
        ('request', 'complete', ('user_prompt:Wait.',)),
        ('response', 'complete', ('call:c1',)),
        ('request', 'interrupted', ()),
        boundary('cancelled: Run cancelled'),
    ]
    assert failure(interrupted, 'r1') == ([True], 'run_cancelled')
    result = following[-1]['result']
    assert isinstance(result, dict)
    assert result['output'] == 'done'
    assert unanswered_calls(requests[-1]) == set()
    assert '[Run ended: cancelled: Run cancelled]' in str(requests[-1])
    assert shape(await store.load(_CONVERSATION_ID)) == [
        *shape(partial[:2]),
        ('request', 'interrupted', ('return:c1:interrupted',)),
        *shape(partial[3:]),
        ('request', 'complete', ('user_prompt:Again.',)),
        ('response', 'complete', ('text:done',)),
    ]
