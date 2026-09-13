import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any, cast

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo
from pytest_mock import MockerFixture

from ovid_core import InMemoryConversationStore
from ovid_core.messages.models import AgentMessage
from ovid_core.runtime import AgentEvent, ConversationId, RunCompletedEvent, RunId
from ovid_core.server import AgentRunRequest, AuthorizationResult, RequestContext, ServerConfig, create_agent_app
from ovid_core.server.active_runs import _ActiveRun
from ovid_core.server.contracts import ASGIMessage, ASGIReceive, ASGIScope, ASGISend
from ovid_core.server.models import AgentRunResponse, ServerErrorResponse
from ovid_core.server.runtime import _AgentServerRuntime
from tests.server.server_helpers import allow, build_registration, server_client


@pytest.mark.parametrize('phase', ['stream', 'persistence', 'anonymous'])
async def test_http_cancellation_is_owned_idempotent_and_releases_capacity(
    mocker: MockerFixture,
    phase: str,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    run_ids: asyncio.Queue[str] = asyncio.Queue()

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del messages, info
        nonlocal calls
        calls += 1
        yield 'Hello'
        if calls == 1 and phase != 'persistence':
            await block()
        yield ' server'

    async def block() -> None:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def append(conversation_id: ConversationId, messages: tuple[AgentMessage, ...]) -> None:
        del conversation_id, messages
        if calls == 1 and phase == 'persistence':
            await block()

    async def authorize(context: RequestContext, _: str) -> AuthorizationResult:
        principal = context.header('authorization')
        return AuthorizationResult(
            allowed=principal in ('owner', 'other', 'anonymous'),
            principal=None if principal == 'anonymous' else principal,
        )

    mocker.patch('tests.server.server_helpers.text_stream', new=stream)
    registration = await build_registration()
    store = mocker.Mock()
    store.load = mocker.AsyncMock(return_value=())
    store.append = mocker.AsyncMock(side_effect=append)
    app = create_agent_app(
        agents=(registration, replace(registration, id='other-agent')),
        authorize=authorize,
        config=ServerConfig(max_concurrency=1),
        store=store,
    )

    async def observed(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        async def capture(message: ASGIMessage) -> None:
            body = message.get('body', b'')
            if body.startswith(b'event: run_started\n'):
                payload = json.loads(body.decode().split('data: ', 1)[1])
                run_ids.put_nowait(payload['run_id'])
            await send(message)

        await app(scope, receive, capture)

    async with server_client(observed) as client:
        request = AgentRunRequest(prompt='Wait.')
        running = asyncio.create_task(
            client.post(
                '/agents/writer/events',
                content=request.model_dump_json(),
                headers={
                    'content-type': 'application/json',
                    'authorization': 'anonymous' if phase == 'anonymous' else 'owner',
                },
            )
        )
        try:
            async with asyncio.timeout(5):
                run_id = await run_ids.get()
                await started.wait()
                path = f'/agents/writer/runs/{run_id}'
                denied = await client.delete(path)
                assert denied.status_code == 403
                anonymous = await client.delete(path, headers={'authorization': 'anonymous'})
                assert anonymous.status_code == 403
                assert not cancelled.is_set()
                if phase == 'anonymous':
                    release.set()
                    result = await running
                    assert 'event: run_result\n' in result.text
                    assert 'event: run_failed\n' not in result.text
                    return
                for target, principal in ((path, 'other'), (f'/agents/other-agent/runs/{run_id}', 'owner')):
                    response = await client.delete(target, headers={'authorization': principal})
                    assert response.status_code == 204
                    assert response.content == b''
                    assert not cancelled.is_set()
                    assert not running.done()
                response = await client.delete(path, headers={'authorization': 'owner'})
                assert response.status_code == 204
                assert response.content == b''
                result = await running
                assert cancelled.is_set()
                events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ')]
                assert sum(event['kind'] == 'run_failed' for event in events) == 1
                assert events[-1]['code'] == 'run_cancelled'
                assert not any(event['kind'] == 'run_result' for event in events)
                assert not any(event['kind'] == 'run_completed' for event in events)
                for stale_id in (run_id, str(RunId.new())):
                    stale = await client.delete(f'/agents/writer/runs/{stale_id}', headers={'authorization': 'owner'})
                    assert stale.status_code == 204
                    assert stale.content == b''
                later = await client.post(
                    '/agents/writer/events',
                    content=request.model_dump_json(),
                    headers={'content-type': 'application/json', 'authorization': 'owner'},
                )
                assert 'event: run_result\n' in later.text
        finally:
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)


async def test_external_task_cancellation_propagates_and_releases_capacity(mocker: MockerFixture) -> None:
    started = asyncio.Event()
    cleaned = asyncio.Event()
    execution: asyncio.Future[asyncio.Task[Any]] = asyncio.get_running_loop().create_future()
    sent: list[AgentEvent | AgentRunResponse | ServerErrorResponse] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del messages, info
        yield 'Hello'
        if cleaned.is_set():
            yield ' server'
            return
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    mocker.patch('tests.server.server_helpers.text_stream', new=stream)
    registration = await build_registration()
    runtime = _AgentServerRuntime(
        agents=(registration,),
        authorize=allow,
        config=ServerConfig(max_concurrency=1),
        store=None,
    )
    context = RequestContext(
        method='POST',
        path='/agents/writer/events',
        headers=(('authorization', 'Bearer allowed'),),
        request_id='external-cancel',
    )

    async def send(event: AgentEvent | AgentRunResponse | ServerErrorResponse) -> None:
        sent.append(event)

    async def execute(operation: _ActiveRun) -> None:
        if not execution.done():
            execution.set_result(cast(asyncio.Task[Any], asyncio.current_task()))
        await runtime.events('writer', AgentRunRequest(prompt='Wait.'), context, send=send, operation=operation)

    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        runtime.start('writer', None, tasks, execute)
        await started.wait()
        task = await execution
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()
        assert not any(isinstance(event, (AgentRunResponse, ServerErrorResponse)) for event in sent)
        await runtime.start('writer', None, tasks, execute).done
        assert isinstance(sent[-1], AgentRunResponse)
        assert sent[-1].output == 'Hello server'
        await runtime.close()


async def test_cancellation_after_persistence_preserves_terminal_delivery() -> None:
    registration = await build_registration()
    store = InMemoryConversationStore()
    runtime = _AgentServerRuntime(
        agents=(registration,),
        authorize=allow,
        config=ServerConfig(),
        store=store,
    )
    context = RequestContext(
        method='POST',
        path='/agents/writer/events',
        headers=(('authorization', 'Bearer allowed'),),
        request_id='terminal-cancel',
    )
    conversation_id = ConversationId.new()
    terminal = asyncio.Event()
    release = asyncio.Event()
    sent: list[AgentEvent | AgentRunResponse | ServerErrorResponse] = []

    async def send(event: AgentEvent | AgentRunResponse | ServerErrorResponse) -> None:
        if isinstance(event, RunCompletedEvent):
            terminal.set()
            await release.wait()
        sent.append(event)

    async def execute(operation: _ActiveRun) -> None:
        await runtime.events(
            'writer',
            AgentRunRequest(prompt='Write.', conversation_id=conversation_id),
            context,
            send=send,
            operation=operation,
        )

    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        operation = runtime.start('writer', None, tasks, execute)
        try:
            await terminal.wait()
            persisted = await store.load(conversation_id)
            assert [message.role for message in persisted] == ['request', 'response']
            await runtime.cancel('writer', operation.run_id, context)
            await runtime.cancel('writer', operation.run_id, context)
        finally:
            release.set()
    assert isinstance(sent[-2], RunCompletedEvent)
    assert isinstance(sent[-1], AgentRunResponse)
    assert sent[-1].output == 'Hello server'
    assert await store.load(conversation_id) == persisted


async def test_http_disconnect_waits_for_cleanup_and_suppresses_terminal_sends(mocker: MockerFixture) -> None:
    started = asyncio.Event()
    cleaned = asyncio.Event()
    disconnect = asyncio.Event()

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del messages, info
        yield 'Hello'
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    mocker.patch('tests.server.server_helpers.text_stream', new=stream)
    registration = await build_registration()
    store = InMemoryConversationStore()
    app = create_agent_app(
        agents=(registration,),
        authorize=allow,
        config=ServerConfig(max_concurrency=1),
        store=store,
    )
    conversation_id = ConversationId.new()
    request = AgentRunRequest(prompt='Wait.', conversation_id=conversation_id)
    incoming: asyncio.Queue[ASGIMessage] = asyncio.Queue()
    incoming.put_nowait({'type': 'http.request', 'body': request.model_dump_json().encode(), 'more_body': False})
    messages: list[ASGIMessage] = []

    async def receive() -> ASGIMessage:
        message = await incoming.get()
        if message['type'] == 'http.disconnect':
            disconnect.set()
        return message

    async def send(message: ASGIMessage) -> None:
        assert not disconnect.is_set()
        messages.append(message)

    scope: ASGIScope = {
        'type': 'http',
        'asgi': {'version': '3.0', 'spec_version': '2.3'},
        'http_version': '1.1',
        'method': 'POST',
        'scheme': 'http',
        'path': '/agents/writer/events',
        'raw_path': b'/agents/writer/events',
        'query_string': b'',
        'root_path': '',
        'headers': [(b'content-type', b'application/json'), (b'authorization', b'Bearer allowed')],
        'server': ('test', 80),
        'client': ('test', 123),
    }
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        running = tasks.create_task(app(scope, receive, send))
        await started.wait()
        incoming.put_nowait({'type': 'http.disconnect'})
        await running
    assert cleaned.is_set()
    assert await store.load(conversation_id) == ()
    body = b''.join(message.get('body', b'') for message in messages)
    assert b'event: run_started\n' in body
    assert b'event: run_completed\n' not in body
    assert b'event: run_result\n' not in body
