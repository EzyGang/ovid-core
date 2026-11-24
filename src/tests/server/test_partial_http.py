import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx2
import pytest
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCalls
from pytest_mock import MockerFixture

from ovid_core import InMemoryConversationStore
from ovid_core.messages import AgentMessage
from ovid_core.persistence import ConversationStore
from ovid_core.runtime import ConversationId
from ovid_core.server import AgentRunRequest, create_ag_ui_app, create_agent_app
from tests.server.server_helpers import allow, server_client
from tests.support.deferred_consumer import SafeTool
from tests.support.partial_consumer import AppendOnlyStore, WaitingTool, registration, returned_calls, shape, tool_call


_HEADERS = {'Authorization': 'Bearer allowed'}
_AG_UI_BODY = {
    'threadId': 'thread-1',
    'runId': 'client-run',
    'state': {},
    'messages': [{'id': 'user', 'role': 'user', 'content': 'Write.'}],
    'tools': [],
    'context': [],
    'forwardedProps': {},
}
_AG_UI_CONVERSATION_ID = ConversationId(root=uuid5(NAMESPACE_URL, 'ovid-ag-ui:writer:thread-1'))


async def _post(client: httpx2.AsyncClient, request: AgentRunRequest) -> list[dict[str, Any]]:
    response = await client.post(
        '/agents/writer/events',
        headers={**_HEADERS, 'Content-Type': 'application/json'},
        content=request.model_dump_json(exclude_none=True),
    )

    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]


async def test_native_http_partial_commit_finishes_before_next_run_on_conversation(mocker: MockerFixture) -> None:
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del info
        requests.append(messages)
        if len(requests) == 1:
            raise ModelAPIError('partial', 'provider down')
        yield 'done'

    entered = asyncio.Event()
    release = asyncio.Event()
    store = InMemoryConversationStore()
    commit = store.commit

    async def slow_commit(
        conversation_id: ConversationId,
        messages: tuple[AgentMessage, ...],
        history: tuple[AgentMessage, ...],
    ) -> None:
        if not entered.is_set():
            entered.set()
            await release.wait()
        await commit(conversation_id, messages, history)

    mocker.patch.object(store, 'commit', side_effect=slow_commit)
    app = create_agent_app(agents=(await registration(stream),), authorize=allow, store=store)
    conversation_id = ConversationId.new()

    async with server_client(app) as client:
        first = asyncio.create_task(_post(client, AgentRunRequest(prompt='Fail.', conversation_id=conversation_id)))
        await entered.wait()
        busy = await _post(client, AgentRunRequest(prompt='Again.', conversation_id=conversation_id))
        release.set()
        failed = await first
        following = await _post(client, AgentRunRequest(prompt='Again.', conversation_id=conversation_id))

    assert busy == [
        {'kind': 'server_error', 'code': 'conversation_busy', 'message': 'Conversation already has an active run'}
    ]
    assert [event['kind'] for event in failed][-2:] == ['run_failed', 'server_error']
    assert failed[-2]['persisted'] is True
    assert following[-1]['output'] == 'done'
    assert len(requests) == 2
    assert '[Run ended: failed: Agent run failed]' in str(requests[-1])


@pytest.mark.parametrize('store_type', [InMemoryConversationStore, AppendOnlyStore])
async def test_ag_ui_persists_partial_transcript(store_type: Callable[[], ConversationStore]) -> None:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        del info
        if returned_calls(messages):
            raise ModelAPIError('partial', 'provider down')
        yield tool_call('safe', 'c1')

    store = store_type()
    app = create_ag_ui_app(agents=(await registration(stream, (SafeTool(),)),), authorize=allow, store=store)
    async with server_client(app) as client:
        response = await client.post('/agents/writer', headers=_HEADERS, json=_AG_UI_BODY)

    assert '"type":"RUN_ERROR"' in response.text
    assert shape(await store.load(_AG_UI_CONVERSATION_ID)) == [
        ('request', 'complete', ('user_prompt:Write.',)),
        ('response', 'complete', ('call:c1',)),
        ('request', 'complete', ('return:c1:success',)),
        ('response', 'interrupted', ()),
        ('response', 'interrupted', ('text:[Run ended: failed: Agent run failed]',)),
    ]


async def test_ag_ui_cancellation_during_tool_persists_partial_transcript() -> None:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        del messages, info
        yield tool_call('wait', 'c1')

    tool = WaitingTool()
    store = InMemoryConversationStore()
    app = create_ag_ui_app(agents=(await registration(stream, (tool,)),), authorize=allow, store=store)

    async with server_client(app) as client:
        request = asyncio.create_task(client.post('/agents/writer', headers=_HEADERS, json=_AG_UI_BODY))
        await tool.started.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

    assert shape(await store.load(_AG_UI_CONVERSATION_ID)) == [
        ('request', 'complete', ('user_prompt:Write.',)),
        ('response', 'complete', ('call:c1',)),
        ('request', 'interrupted', ()),
        ('response', 'interrupted', ('text:[Run ended: cancelled: Run cancelled]',)),
    ]
