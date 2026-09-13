import asyncio
import json
from collections.abc import AsyncIterator
from functools import partial
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo
from pytest_mock import MockerFixture

from ovid_core.messages.models import AgentMessage
from ovid_core.runtime import ConversationId, RunId
from ovid_core.server import (
    AgentRunRequest,
    AuthorizationResult,
    RequestContext,
    ServerConfig,
    StdioCancelRequest,
    StdioInitializeRequest,
    StdioRunRequest,
    create_stdio_server,
)
from ovid_core.server.active_runs import _ConnectionOwner
from ovid_core.server.stdio_connection import _StdioConnection
from tests.server.server_helpers import build_registration
from tests.server.test_stdio_server import _frame


@pytest.mark.parametrize('persistence,termination', [(False, b''), (False, b'x' * 501), (True, b'')])
async def test_stdio_input_termination_cancels_before_shutdown(
    mocker: MockerFixture,
    persistence: bool,
    termination: bytes,
) -> None:
    blocked = asyncio.Event()
    lifecycle: list[str] = []

    async def wait() -> None:
        blocked.set()
        try:
            await asyncio.Event().wait()
        finally:
            lifecycle.append('cancelled')

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del messages, info
        yield 'Hello'
        if not persistence:
            await wait()
        yield ' server'

    async def append(conversation_id: ConversationId, messages: tuple[AgentMessage, ...]) -> None:
        del conversation_id, messages
        await wait()

    async def authorize(_: RequestContext, __: str) -> AuthorizationResult:
        return AuthorizationResult(allowed=True)

    async def shutdown() -> None:
        lifecycle.append('shutdown')

    mocker.patch('tests.server.server_helpers.text_stream', new=stream)
    registration = await build_registration()
    store = mocker.Mock()
    store.load = mocker.AsyncMock(return_value=())
    store.append = mocker.AsyncMock(side_effect=append)
    server = create_stdio_server(
        agents=(registration,),
        authorize=authorize,
        store=store if persistence else None,
        shutdown=shutdown,
        config=ServerConfig(max_body_bytes=500),
    )
    request = StdioRunRequest(request_id='run', agent_id='writer', request=AgentRunRequest(prompt='Wait.'))
    first = True

    async def read(_: int) -> bytes:
        nonlocal first
        if first:
            first = False
            return _frame(request)
        await blocked.wait()
        return termination

    frames: list[dict[str, Any]] = []

    async def write(payload: bytes) -> None:
        frames.append(json.loads(payload))

    async with asyncio.timeout(3):
        await server._serve(reader=read, writer=write)

    assert lifecycle == ['cancelled', 'shutdown']
    events = [frame['event']['kind'] for frame in frames if frame['type'] == 'event']
    assert events.count('run_failed') == 1
    assert 'run_completed' not in events
    assert not any(frame['type'] == 'run_result' for frame in frames)
    assert frames[-1]['error']['code'] == 'run_cancelled'


async def test_stdio_cancel_failure_is_correlated_and_does_not_break_later_frames(mocker: MockerFixture) -> None:
    blocked = asyncio.Event()

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del messages, info
        yield 'Hello'
        blocked.set()
        await asyncio.Event().wait()

    async def authorize(_: RequestContext, __: str) -> AuthorizationResult:
        return AuthorizationResult(allowed=True)

    mocker.patch('tests.server.server_helpers.text_stream', new=stream)
    registration = await build_registration()
    server = create_stdio_server(agents=(registration,), authorize=authorize)
    cancel = server._runtime.cancel_owned
    attempts = 0

    async def fail_once(run_id: RunId, owner: _ConnectionOwner) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError('private cancellation failure')
        await cancel(run_id, owner)

    mocker.patch.object(server._runtime, 'cancel_owned', side_effect=fail_once)
    incoming: asyncio.Queue[bytes] = asyncio.Queue()
    outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def read(_: int) -> bytes:
        return await incoming.get()

    async def write(payload: bytes) -> None:
        outgoing.put_nowait(json.loads(payload))

    task = asyncio.create_task(server._serve(reader=read, writer=write))
    try:
        async with asyncio.timeout(3):
            incoming.put_nowait(
                _frame(
                    StdioRunRequest(
                        request_id='run',
                        agent_id='writer',
                        request=AgentRunRequest(prompt='Wait.'),
                    )
                )
            )
            await blocked.wait()
            incoming.put_nowait(_frame(StdioCancelRequest(request_id='failed-cancel', target_request_id='run')))
            incoming.put_nowait(_frame(StdioInitializeRequest(request_id='probe')))
            while (frame := await outgoing.get())['type'] == 'event':
                pass
            assert frame['request_id'] == 'failed-cancel'
            assert frame['error']['code'] == 'internal_error'
            assert 'private cancellation failure' not in json.dumps(frame)
            frame = await outgoing.get()
            assert frame['request_id'] == 'probe'
            assert frame['type'] == 'initialized'
            incoming.put_nowait(_frame(StdioCancelRequest(request_id='cancel', target_request_id='run')))
            while (frame := await outgoing.get())['type'] == 'event':
                pass
            assert frame['request_id'] == 'run'
            assert frame['error']['code'] == 'run_cancelled'
            incoming.put_nowait(b'')
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_run_scheduling_failure_releases_queue_and_completed_request_id(mocker: MockerFixture) -> None:
    frames: list[dict[str, Any]] = []

    async def write(payload: bytes) -> None:
        frames.append(json.loads(payload))

    authorize = mocker.AsyncMock(return_value=AuthorizationResult(allowed=True))
    server = create_stdio_server(agents=(), authorize=authorize)
    tasks = asyncio.TaskGroup()
    connection = _StdioConnection(
        agents=(),
        commands={},
        authorize=authorize,
        config=ServerConfig(),
        runtime=server._runtime,
        writer=write,
        tasks=tasks,
    )
    request = _frame(
        StdioRunRequest(
            request_id='run',
            agent_id='missing',
            request=AgentRunRequest(prompt='Run.'),
        )
    )
    async with asyncio.timeout(1):
        await connection.receive(request)
        await server._runtime.close()
        loop = asyncio.get_running_loop()
        mocker.patch.object(loop, 'create_task', side_effect=partial(loop.create_task, eager_start=True))
        async with tasks:
            await connection.receive(request)
            await connection.receive(request)
        await server._runtime.close()

    assert [frame['request_id'] for frame in frames] == ['run', 'run', 'run']
    assert [frame['error']['code'] for frame in frames] == [
        'internal_error',
        'agent_not_found',
        'agent_not_found',
    ]
