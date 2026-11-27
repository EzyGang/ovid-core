import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Literal

import httpx2
import pytest
from pydantic import JsonValue
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo

from ovid_core import InMemoryConversationStore
from ovid_core.journal import RunStartedEntry
from ovid_core.messages import AgentMessage
from ovid_core.persistence import ConversationStore
from ovid_core.runtime import ConversationId, RunId
from ovid_core.server import AgentRunRequest, ServerConfig, StdioRunRequest, create_agent_app
from tests.server.server_helpers import allow, server_client
from tests.support.journal_consumer import PlainHistoryStore
from tests.support.partial_consumer import StdioStep, registration, stdio_session


type Transport = Literal['stdio', 'http']
type Payload = dict[str, JsonValue]

_TRANSPORTS = pytest.mark.parametrize('transport', ['stdio', 'http'])


class CountingModel:
    """Count the model calls. A call waits for the gate, and the first `failures` calls fail."""

    def __init__(self, *, failures: int = 0) -> None:
        self.calls = 0
        self.failures = failures
        self.started = asyncio.Event()
        self.gate = asyncio.Event()
        self.gate.set()

    async def stream(self, messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del messages, info
        self.calls += 1
        self.started.set()
        await self.gate.wait()
        if self.calls <= self.failures:
            raise RuntimeError('model down')
        yield 'done'


class BlockedCommitStore(InMemoryConversationStore):
    """Hold each history commit until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def commit(
        self,
        conversation_id: ConversationId,
        messages: tuple[AgentMessage, ...],
        history: tuple[AgentMessage, ...],
    ) -> None:
        await self.release.wait()
        await super().commit(conversation_id, messages, history)


def _request(conversation_id: ConversationId, key: str | None, *, resume: bool = False) -> AgentRunRequest:
    prompt = None if resume else 'Go.'
    return AgentRunRequest(prompt=prompt, resume=resume, conversation_id=conversation_id, client_request_id=key)


def _stdio(request_id: str, request: AgentRunRequest) -> StdioRunRequest:
    return StdioRunRequest(request_id=request_id, agent_id='writer', request=request)


async def _http_terminal(client: httpx2.AsyncClient, request: AgentRunRequest) -> Payload:
    response = await client.post(
        '/agents/writer/events',
        headers={'Authorization': 'Bearer allowed', 'Content-Type': 'application/json'},
        content=request.model_dump_json(exclude_none=True),
    )
    data = [line.removeprefix('data: ') for line in response.text.splitlines() if line.startswith('data: ')]
    terminal: Payload = json.loads(data[-1])
    del terminal['kind']

    return terminal


async def _terminals(
    transport: Transport,
    model: CountingModel,
    store: ConversationStore,
    *requests: AgentRunRequest,
) -> list[Payload]:
    """Send the requests one after another, and return the result or the error of each one."""
    agent = await registration(model.stream)
    if transport == 'http':
        async with server_client(create_agent_app(agents=(agent,), authorize=allow, store=store)) as client:
            return [await _http_terminal(client, request) for request in requests]

    steps: list[StdioStep] = []
    for index, request in enumerate(requests):
        steps += [_stdio(f'r{index}', request), f'r{index}']
    frames = await stdio_session(agent, store, *steps)

    return [payload for frame in frames if isinstance(payload := frame.get('result', frame.get('error')), dict)]


@_TRANSPORTS
async def test_same_key_after_completion_is_refused_without_a_model_call_or_a_write(transport: Transport) -> None:
    conversation_id = ConversationId.new()
    store = InMemoryConversationStore()
    model = CountingModel()

    first, again = await _terminals(
        transport, model, store, _request(conversation_id, 'k1'), _request(conversation_id, 'k1')
    )

    journal = await store.load_journal(conversation_id)
    assert first['output'] == 'done'
    assert again == {
        'code': 'duplicate_request',
        'message': 'Client request ID was already used',
        'run_id': first['run_id'],
    }
    assert model.calls == 1
    assert len(journal.messages) == 2
    assert [(str(run.run_id), run.outcome, run.client_request_id) for run in journal.runs] == [
        (first['run_id'], 'completed', 'k1')
    ]


@_TRANSPORTS
async def test_same_key_after_a_lost_run_names_resume_before_the_resume_checks(transport: Transport) -> None:
    conversation_id = ConversationId.new()
    store = InMemoryConversationStore()
    lost = RunId.new()
    await store.append_entries(
        conversation_id, (RunStartedEntry(run_id=lost, at=datetime.now(UTC), client_request_id='k1'),)
    )
    model = CountingModel()

    results = await _terminals(
        transport, model, store, _request(conversation_id, 'k1'), _request(conversation_id, 'k1', resume=True)
    )

    message = 'Client request ID was already used by a run that did not finish. Send a resume request.'
    assert results == [{'code': 'duplicate_request', 'message': message, 'run_id': str(lost)}] * 2
    assert model.calls == 0
    assert [run.outcome for run in (await store.load_journal(conversation_id)).runs] == [None]


@_TRANSPORTS
async def test_same_key_after_a_stopped_run_with_a_partial_commit_names_resume(transport: Transport) -> None:
    conversation_id = ConversationId.new()
    store = InMemoryConversationStore()
    model = CountingModel(failures=1)

    failed, again = await _terminals(
        transport, model, store, _request(conversation_id, 'k1'), _request(conversation_id, 'k1')
    )

    runs = (await store.load_journal(conversation_id)).runs
    assert failed['code'] == 'agent_run_failed'
    assert 'run_id' not in failed
    assert (again['code'], again['run_id']) == ('duplicate_request', str(runs[0].run_id))
    assert 'resume' in str(again['message'])
    assert [run.outcome for run in runs] == ['failed']
    assert model.calls == 1


@_TRANSPORTS
async def test_requests_without_a_key_and_the_same_key_on_another_conversation_run(transport: Transport) -> None:
    conversation_id = ConversationId.new()
    store = InMemoryConversationStore()
    model = CountingModel()

    results = await _terminals(
        transport,
        model,
        store,
        _request(conversation_id, None),
        _request(conversation_id, None),
        _request(conversation_id, 'k1'),
        _request(ConversationId.new(), 'k1'),
    )

    assert [result['output'] for result in results] == ['done'] * 4
    assert model.calls == 4


@_TRANSPORTS
async def test_a_store_without_a_journal_ignores_the_key(transport: Transport) -> None:
    conversation_id = ConversationId.new()
    model = CountingModel()

    results = await _terminals(
        transport, model, PlainHistoryStore(), _request(conversation_id, 'k1'), _request(conversation_id, 'k1')
    )

    assert [result['output'] for result in results] == ['done'] * 2
    assert model.calls == 2


async def test_same_key_while_a_stopped_stdio_run_still_commits_returns_busy_with_that_run() -> None:
    """A stdio connection runs its requests in order, so its runs on a conversation overlap only in a partial commit."""
    conversation_id = ConversationId.new()
    model = CountingModel(failures=1)
    store = BlockedCommitStore()

    frames = await stdio_session(
        await registration(model.stream),
        store,
        _stdio('r0', _request(conversation_id, 'k1')),
        'r0',
        _stdio('r1', _request(conversation_id, 'k1')),
        'r1',
        store.release.set,
        config=ServerConfig(shutdown_grace_seconds=1),
    )

    errors = {frame['request_id']: frame['error'] for frame in frames if frame['type'] == 'error'}
    started = next(frame['event'] for frame in frames if frame['type'] == 'event')
    assert isinstance(started, dict)
    assert errors['r1'] == {
        'code': 'conversation_busy',
        'message': 'Conversation already has an active run',
        'run_id': started['run_id'],
    }
    assert model.calls == 1


async def test_same_key_during_an_active_http_run_returns_busy_with_the_active_run() -> None:
    conversation_id = ConversationId.new()
    model = CountingModel()
    model.gate.clear()
    agent = await registration(model.stream)
    app = create_agent_app(agents=(agent,), authorize=allow, store=InMemoryConversationStore())

    async with server_client(app) as client:
        first = asyncio.create_task(_http_terminal(client, _request(conversation_id, 'k1')))
        await model.started.wait()
        busy = await _http_terminal(client, _request(conversation_id, 'k1'))
        model.gate.set()
        result = await first

    assert (busy['code'], busy['run_id']) == ('conversation_busy', result['run_id'])
    assert model.calls == 1
