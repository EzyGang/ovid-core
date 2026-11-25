import asyncio
import json
import threading
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCalls

from ovid_core import InMemoryConversationStore
from ovid_core.messages import AgentMessage
from ovid_core.runtime import ConversationId
from ovid_core.server import AgentRunRequest, ServerConfig, StdioCancelRequest, StdioRunRequest, create_agent_app
from tests.server.server_helpers import allow, server_client
from tests.support.partial_consumer import (
    StdioStep,
    WaitingTool,
    boundary,
    registration,
    shape,
    stdio_session,
    tool_call,
)


_OUTCOMES = {'timeout': 'timeout', 'run_cancelled': 'cancelled'}


class ThreadStore(InMemoryConversationStore):
    """Write in a worker thread that blocks until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.committed = asyncio.Event()

    async def commit(
        self,
        conversation_id: ConversationId,
        messages: tuple[AgentMessage, ...],
        history: tuple[AgentMessage, ...],
    ) -> None:
        await asyncio.to_thread(self.release.wait)
        await super().commit(conversation_id, messages, history)
        self.committed.set()


@pytest.mark.parametrize('stop', ['request-timeout', 'stdin-eof', 'client-cancel'])
async def test_stdio_stored_outcome_matches_sent_error(stop: str) -> None:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        del messages, info
        yield tool_call('wait', 'c1')

    conversation_id = ConversationId.new()
    store = InMemoryConversationStore()
    tool = WaitingTool()
    run = StdioRunRequest(
        request_id='r1',
        agent_id='writer',
        request=AgentRunRequest(prompt='Wait.', conversation_id=conversation_id),
    )
    steps: dict[str, tuple[StdioStep, ...]] = {
        'request-timeout': (run, 'r1'),
        'stdin-eof': (run, tool.started),
        'client-cancel': (run, tool.started, StdioCancelRequest(request_id='c', target_request_id='r1'), 'r1'),
    }
    config = ServerConfig(request_timeout_seconds=0.3 if stop == 'request-timeout' else 60)

    frames = await stdio_session(await registration(stream, (tool,)), store, *steps[stop], config=config)

    error = frames[-1]['error']
    assert isinstance(error, dict)
    failed = frames[-2]['event']
    assert isinstance(failed, dict)
    stored = shape(await store.load(conversation_id))
    assert stored[-1] == boundary(f'{_OUTCOMES[str(error["code"])]}: {error["message"]}')
    assert failed['message'] == error['message']
    assert (error['code'], failed['persisted']) == ('timeout' if stop == 'request-timeout' else 'run_cancelled', True)


async def test_commit_past_grace_keeps_conversation_busy_until_thread_write_ends() -> None:
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del info
        requests.append(messages)
        if len(requests) == 1:
            raise ModelAPIError('partial', 'provider down')
        yield 'done'

    store = ThreadStore()
    config = ServerConfig(shutdown_grace_seconds=1)
    app = create_agent_app(agents=(await registration(stream),), authorize=allow, config=config, store=store)
    conversation_id = ConversationId.new()

    async with server_client(app) as client:
        failed = await _post(client, AgentRunRequest(prompt='Fail.', conversation_id=conversation_id))
        busy = await _post(client, AgentRunRequest(prompt='Again.', conversation_id=conversation_id))
        store.release.set()
        await asyncio.wait_for(store.committed.wait(), 5)
        following = await _post(client, AgentRunRequest(prompt='Again.', conversation_id=conversation_id))

    assert [(event['kind'], event.get('persisted')) for event in failed][-2:] == [
        ('run_failed', False),
        ('server_error', None),
    ]
    assert busy[-1]['code'] == 'conversation_busy'
    assert following[-1]['output'] == 'done'
    assert '[Run ended: failed: Agent run failed]' in str(requests[-1])


async def _post(client: httpx2.AsyncClient, request: AgentRunRequest) -> list[dict[str, Any]]:
    response = await client.post(
        '/agents/writer/events',
        headers={'Authorization': 'Bearer allowed', 'Content-Type': 'application/json'},
        content=request.model_dump_json(exclude_none=True),
    )

    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
