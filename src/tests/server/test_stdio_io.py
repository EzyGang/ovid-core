import asyncio
import json
from typing import Any

import pytest
from pytest_mock import MockerFixture

import ovid_core.server.stdio as stdio
from ovid_core import InMemoryConversationStore
from ovid_core.runtime import ConversationId
from ovid_core.server import (
    AgentRunRequest,
    AuthorizationResult,
    RequestContext,
    ServerConfig,
    StdioCancelRequest,
    StdioCommandRequest,
    StdioInitializeRequest,
    StdioRunRequest,
    create_stdio_server,
)
from ovid_core.server.stdio_connection import _StdioConnection
from tests.server.server_helpers import build_registration
from tests.server.test_stdio_server import _frame


async def test_stdio_launcher_uses_bounded_binary_streams(mocker: MockerFixture) -> None:
    async def authorize(_: RequestContext, __: str) -> AuthorizationResult:
        return AuthorizationResult(allowed=True)

    config = ServerConfig()
    server = create_stdio_server(agents=(await build_registration(),), authorize=authorize, config=config)
    request = f'{StdioInitializeRequest(request_id="init").model_dump_json()}\n'.encode()
    stdin = mocker.Mock()
    stdin.buffer.readline.side_effect = (request, b'')
    stdout = mocker.Mock()
    mocker.patch.object(stdio.sys, 'stdin', stdin)
    mocker.patch.object(stdio.sys, 'stdout', stdout)

    await server.run()

    assert stdin.buffer.readline.call_count == 2
    stdin.buffer.readline.assert_called_with(config.max_body_bytes + 1)
    stdout.buffer.write.assert_called_once()
    stdout.buffer.flush.assert_called_once_with()


async def test_stdio_failed_error_write_keeps_dispatch_correlated_and_queue_moving(mocker: MockerFixture) -> None:
    frames: list[dict[str, Any]] = []
    fail = True

    async def write(payload: bytes) -> None:
        nonlocal fail
        if fail:
            fail = False
            raise OSError('private write failure')
        frames.append(json.loads(payload))

    authorize = mocker.AsyncMock(return_value=AuthorizationResult(allowed=True))
    server = create_stdio_server(agents=(), authorize=authorize)
    async with asyncio.timeout(1), asyncio.TaskGroup() as tasks:
        connection = _StdioConnection(
            agents=(),
            commands={},
            authorize=authorize,
            config=ServerConfig(),
            runtime=server._runtime,
            writer=write,
            tasks=tasks,
        )
        await connection.receive(_frame(StdioCommandRequest(request_id='failed', command_id='missing')))
        await connection.receive(_frame(StdioCommandRequest(request_id='later', command_id='missing')))

    assert [frame['request_id'] for frame in frames] == ['failed', 'later']
    assert [frame['error']['code'] for frame in frames] == ['internal_error', 'command_not_found']
    assert 'private write failure' not in json.dumps(frames)


@pytest.mark.parametrize('terminal', [False, True])
async def test_stdio_cancel_preserves_inflight_frame_and_durable_completion(
    mocker: MockerFixture,
    terminal: bool,
) -> None:
    blocked = asyncio.Event()
    release = asyncio.Event()
    frames: list[dict[str, Any]] = []
    wire = bytearray()

    async def write(payload: bytes) -> None:
        frame = json.loads(payload)
        midpoint = len(payload) // 2
        wire.extend(payload[:midpoint])
        if not blocked.is_set() and (not terminal or frame.get('event', {}).get('kind') == 'run_completed'):
            blocked.set()
            await release.wait()
        wire.extend(payload[midpoint:])
        frames.append(frame)

    registration = await build_registration()
    authorize = mocker.AsyncMock(return_value=AuthorizationResult(allowed=True))
    store = InMemoryConversationStore()
    conversation_id = ConversationId.new()
    server = create_stdio_server(agents=(registration,), authorize=authorize, store=store)
    async with asyncio.timeout(3), asyncio.TaskGroup() as tasks:
        connection = _StdioConnection(
            agents=(registration,),
            commands={},
            authorize=authorize,
            config=ServerConfig(),
            runtime=server._runtime,
            writer=write,
            tasks=tasks,
        )
        await connection.receive(
            _frame(
                StdioRunRequest(
                    request_id='run',
                    agent_id='writer',
                    request=AgentRunRequest(prompt='Write.', conversation_id=conversation_id),
                )
            )
        )
        await blocked.wait()
        cancellation = tasks.create_task(
            connection.receive(
                _frame(
                    StdioCancelRequest(
                        request_id='cancel',
                        target_request_id='run',
                    )
                )
            ),
            eager_start=True,
        )
        probe = tasks.create_task(
            connection.receive(_frame(StdioInitializeRequest(request_id='probe'))),
            eager_start=True,
        )
        await asyncio.sleep(0)
        try:
            assert cancellation.done() is terminal
            assert not probe.done()
        finally:
            release.set()

    assert [json.loads(line) for line in wire.splitlines()] == frames
    run_frames = [frame for frame in frames if frame['request_id'] == 'run']
    assert any(frame['request_id'] == 'probe' and frame['type'] == 'initialized' for frame in frames)
    if terminal:
        assert run_frames[-1]['type'] == 'run_result'
        assert not any(frame['type'] == 'error' for frame in run_frames)
        assert len(await store.load(conversation_id)) == 2
    else:
        assert run_frames[-1]['error']['code'] == 'run_cancelled'
        assert not any(frame['type'] == 'run_result' for frame in run_frames)
        assert await store.load(conversation_id) == ()
