import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from pydantic import JsonValue
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo
from pytest_mock import MockerFixture

from ovid_core import InMemoryConversationStore, PersistenceError
from ovid_core.messages import AgentMessage, TextPart, UserPromptPart
from ovid_core.runtime import ConversationId
from ovid_core.server import AgentRunRequest, StdioCancelRequest, StdioRunRequest
from ovid_core.usage import RequestUsage
from tests.support.partial_consumer import boundary, failure, registration, shape, stdio_session


_CONVERSATION_ID = ConversationId.new()


def _run(request_id: str, prompt: str) -> StdioRunRequest:
    return StdioRunRequest(
        request_id=request_id,
        agent_id='writer',
        request=AgentRunRequest(prompt=prompt, conversation_id=_CONVERSATION_ID),
    )


async def _failing_stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
    del messages, info
    raise ModelAPIError('partial', 'provider down')
    yield 'unreachable'


async def test_stdio_commit_failure_keeps_run_error_and_reports_not_persisted(mocker: MockerFixture) -> None:
    store = InMemoryConversationStore()
    mocker.patch.object(store, 'commit', side_effect=PersistenceError('private store failure'))
    agent = await registration(_failing_stream)

    frames = await stdio_session(agent, store, _run('r1', 'Write.'), 'r1')

    assert failure(frames, 'r1') == ([False], 'agent_run_failed')
    assert 'private' not in str(frames)
    assert await store.load(_CONVERSATION_ID) == ()


@pytest.mark.parametrize(('cause', 'run_failed'), [('suspended-history', [False]), ('upstream-start', [])])
async def test_stdio_failure_before_any_run_message_persists_nothing(
    mocker: MockerFixture,
    cause: str,
    run_failed: list[JsonValue],
) -> None:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del messages, info
        yield 'unreachable'

    now = datetime.now(UTC)
    history = (
        AgentMessage(role='request', parts=(UserPromptPart(content='Start.'),), timestamp=now),
        AgentMessage(
            role='response',
            parts=(TextPart(content='Working'),),
            timestamp=now,
            request_usage=RequestUsage(),
            state='suspended',
        ),
    )
    store = InMemoryConversationStore()
    await store.append(_CONVERSATION_ID, history)
    agent = await registration(stream)
    if cause == 'upstream-start':
        runtime = agent.agent._runtime_for_adapter()
        mocker.patch.object(runtime.upstream_agent, 'run_stream_events', side_effect=ValueError('private'))

    frames = await stdio_session(agent, store, _run('r1', 'Next.'), 'r1')

    assert failure(frames, 'r1') == (run_failed, 'agent_run_failed')
    assert await store.load(_CONVERSATION_ID) == history


async def test_stdio_cancel_during_partial_commit_waits_for_the_commit(mocker: MockerFixture) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    store = InMemoryConversationStore()
    commit = store.commit

    async def slow_commit(
        conversation_id: ConversationId,
        messages: tuple[AgentMessage, ...],
        history: tuple[AgentMessage, ...],
    ) -> None:
        entered.set()
        await release.wait()
        await commit(conversation_id, messages, history)

    mocker.patch.object(store, 'commit', side_effect=slow_commit)
    agent = await registration(_failing_stream)
    cancel = StdioCancelRequest(request_id='cancel', target_request_id='r1')
    loop = asyncio.get_running_loop()

    def release_after_cancel() -> None:
        loop.call_soon(release.set)

    frames = await stdio_session(agent, store, _run('r1', 'Write.'), entered, release_after_cancel, cancel, 'r1')

    assert failure(frames, 'r1') == ([True], 'agent_run_failed')
    assert shape(await store.load(_CONVERSATION_ID))[-1] == boundary('failed: Agent run failed')
