import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from typing import Protocol, cast

import pytest
from pydantic import JsonValue, TypeAdapter
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo
from pytest_mock import MockerFixture

from ovid_core import AgentRunError, InMemoryConversationStore, PersistenceError
from ovid_core.runtime import ConversationId
from ovid_core.server import (
    AgentRunRequest,
    AuthorizationResult,
    CommandRegistration,
    RequestContext,
    ServerConfig,
    StdioCancelRequest,
    StdioCommandRequest,
    StdioInitializeRequest,
    StdioRunRequest,
    create_stdio_server,
)
from ovid_core.server.stdio import StdioAgentServer
from tests.server.server_helpers import build_registration
from tests.support.agent_consumer import AgentDependencies


class _MockCall(Protocol):
    @property
    def args(self) -> tuple[bytes, ...]: ...


class _WriterMock(Protocol):
    @property
    def await_args_list(self) -> list[_MockCall]: ...


_PAYLOAD_ADAPTER = TypeAdapter(dict[str, JsonValue])


def _frame(request: StdioInitializeRequest | StdioRunRequest | StdioCommandRequest | StdioCancelRequest) -> bytes:
    return f'{request.model_dump_json()}\n'.encode()


def _payloads(writer: _WriterMock) -> list[dict[str, JsonValue]]:
    return [_PAYLOAD_ADAPTER.validate_json(call.args[0]) for call in writer.await_args_list]


async def test_stdio_streams_runs_discovers_and_dispatches_registered_commands(mocker: MockerFixture) -> None:
    registration = await build_registration()
    store = InMemoryConversationStore()
    lifecycle: list[str] = []
    authorized: list[str] = []
    conversation_id = ConversationId.new()

    async def authorize(context: RequestContext, resource_id: str) -> AuthorizationResult:
        assert context.method == 'STDIO'
        authorized.append(resource_id)

        return AuthorizationResult(allowed=True, principal='stdio-user')

    async def inspect(
        context: RequestContext,
        authorization: AuthorizationResult,
        arguments: JsonValue,
    ) -> JsonValue:
        assert context.path == '/commands/inspect'
        assert authorization.principal == 'stdio-user'

        return {'arguments': arguments, 'status': 'ok'}

    async def startup() -> None:
        lifecycle.append('startup')

    async def shutdown() -> None:
        lifecycle.append('shutdown')

    server = create_stdio_server(
        agents=(registration,),
        commands=(CommandRegistration(id='inspect', description='Inspect state.', handler=inspect),),
        authorize=authorize,
        store=store,
        startup=startup,
        shutdown=shutdown,
    )
    writer = await _serve_frames(
        server,
        mocker,
        'command',
        (
            _frame(StdioInitializeRequest(request_id='initialize')),
            _frame(
                StdioRunRequest(
                    request_id='run',
                    agent_id='writer',
                    request=AgentRunRequest(prompt='Write.', conversation_id=conversation_id),
                )
            ),
            _frame(StdioCommandRequest(request_id='command', command_id='inspect', arguments={'value': 3})),
        ),
    )

    payloads = _payloads(writer)
    initialized = payloads[0]
    run_payloads = [payload for payload in payloads if payload['request_id'] == 'run']
    command_result = next(payload for payload in payloads if payload['request_id'] == 'command')

    assert lifecycle == ['startup', 'shutdown']
    assert initialized['type'] == 'initialized'
    assert initialized['agents'] == [{'id': 'writer', 'description': 'Write a short response.'}]
    assert initialized['commands'] == [{'id': 'inspect', 'description': 'Inspect state.'}]
    assert [payload['type'] for payload in run_payloads][-1] == 'run_result'
    assert any(payload.get('event', {}).get('kind') == 'text_delta' for payload in run_payloads)
    assert command_result['result'] == {'arguments': {'value': 3}, 'status': 'ok'}
    assert authorized == ['writer', 'command:inspect']
    assert len(await store.load(conversation_id)) == 2


async def test_stdio_normalizes_invalid_denied_failed_and_oversized_requests(mocker: MockerFixture) -> None:
    registration = await build_registration()

    async def authorize(_: RequestContext, resource_id: str) -> AuthorizationResult:
        if resource_id == 'command:core':
            raise PersistenceError('private persistence failure')
        if resource_id == 'command:internal':
            raise ValueError('private internal failure')

        return AuthorizationResult(allowed=resource_id != 'command:denied')

    async def command(_: RequestContext, __: AuthorizationResult, arguments: JsonValue) -> JsonValue:
        if arguments == 'fail':
            raise ValueError('private command failure')
        if arguments == 'slow':
            await asyncio.sleep(1)
        if arguments == 'invalid':
            return cast(JsonValue, object())

        return arguments

    commands = tuple(
        CommandRegistration(id=name, description=f'{name} command.', handler=command)
        for name in ('denied', 'fail', 'slow', 'invalid', 'core', 'internal')
    )
    server = create_stdio_server(
        agents=(registration,),
        commands=commands,
        authorize=authorize,
        config=ServerConfig(max_body_bytes=500, request_timeout_seconds=0.01),
    )
    requests = [
        b'not-json\n',
        _frame(StdioRunRequest(request_id='agent', agent_id='missing', request=AgentRunRequest(prompt='Run.'))),
        _frame(StdioCommandRequest(request_id='missing', command_id='missing')),
        *(
            _frame(StdioCommandRequest(request_id=name, command_id=name, arguments=name))
            for name in ('denied', 'fail', 'slow', 'invalid', 'core', 'internal')
        ),
        b'x' * 501,
    ]
    writer = await _serve_frames(server, mocker, 'internal', requests[:-1], ending=requests[-1])

    errors = {payload['request_id']: payload['error'] for payload in _payloads(writer)}

    assert errors[None]['code'] == 'request_too_large'
    assert errors['agent']['code'] == 'agent_not_found'
    assert errors['missing']['code'] == 'command_not_found'
    assert errors['denied']['code'] == 'forbidden'
    assert errors['fail']['code'] == 'command_failed'
    assert errors['slow']['code'] == 'timeout'
    assert errors['invalid']['code'] == 'command_failed'
    assert errors['core']['code'] == 'server_failure'
    assert errors['internal']['code'] == 'internal_error'
    assert 'private' not in json.dumps(errors)


async def test_stdio_propagates_cancellation_and_normalizes_agent_failures(mocker: MockerFixture) -> None:
    registration = await build_registration()

    async def authorize(_: RequestContext, __: str) -> AuthorizationResult:
        return AuthorizationResult(allowed=True)

    server = create_stdio_server(agents=(registration,), authorize=authorize)
    mocker.patch.object(registration.agent, 'stream', side_effect=AgentRunError('safe agent failure'))
    writer = await _serve_frames(
        server,
        mocker,
        'run',
        (_frame(StdioRunRequest(request_id='run', agent_id='writer', request=AgentRunRequest(prompt='Run.'))),),
    )

    assert _payloads(writer)[0]['error'] == {'code': 'agent_run_failed', 'message': 'safe agent failure'}

    async def wait(_: RequestContext, __: AuthorizationResult, ___: JsonValue) -> JsonValue:
        await asyncio.Event().wait()
        return None

    cancelled = create_stdio_server(
        agents=(registration,),
        commands=(CommandRegistration(id='wait', description='Wait.', handler=wait),),
        authorize=authorize,
    )
    cancelled_reader = mocker.AsyncMock(
        side_effect=(_frame(StdioCommandRequest(request_id='wait', command_id='wait')), b'')
    )
    task = asyncio.create_task(cancelled._serve(reader=cancelled_reader, writer=mocker.AsyncMock()))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_stdio_cancels_only_targeted_run_and_keeps_frames_and_server_intact(mocker: MockerFixture) -> None:
    started = asyncio.Event()
    calls = 0
    principal = 'owner'

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del messages, info
        nonlocal calls
        calls += 1
        yield 'Hello'
        if calls == 1:
            started.set()
            await asyncio.Event().wait()
        yield ' server'

    mocker.patch('tests.server.server_helpers.text_stream', new=stream)
    registration = await build_registration()

    async def authorize(_: RequestContext, __: str) -> AuthorizationResult:
        return AuthorizationResult(allowed=True, principal=principal)

    async def command(_: RequestContext, __: AuthorizationResult, ___: JsonValue) -> JsonValue:
        return 'ordered'

    incoming: asyncio.Queue[bytes] = asyncio.Queue()
    outgoing: asyncio.Queue[dict[str, JsonValue]] = asyncio.Queue()
    wire = bytearray()

    async def read(_: int) -> bytes:
        return await incoming.get()

    async def write(payload: bytes) -> None:
        midpoint = len(payload) // 2
        wire.extend(payload[:midpoint])
        await asyncio.sleep(0)
        wire.extend(payload[midpoint:])
        outgoing.put_nowait(_PAYLOAD_ADAPTER.validate_json(payload))

    server = create_stdio_server(
        agents=(registration,),
        commands=(CommandRegistration(id='inspect', description='Inspect.', handler=command),),
        authorize=authorize,
    )
    task = asyncio.create_task(
        server._serve(reader=mocker.AsyncMock(side_effect=read), writer=mocker.AsyncMock(side_effect=write))
    )
    first = StdioRunRequest(request_id='first', agent_id='writer', request=AgentRunRequest(prompt='Wait.'))
    second = StdioRunRequest(request_id='second', agent_id='writer', request=AgentRunRequest(prompt='Run.'))
    received: list[dict[str, JsonValue]] = []
    try:
        async with asyncio.timeout(5):
            incoming.put_nowait(_frame(first))
            await started.wait()
            incoming.put_nowait(_frame(first))
            while (frame := await outgoing.get())['type'] != 'error':
                received.append(frame)
            received.append(frame)
            assert frame['request_id'] == 'first'
            error = frame['error']
            assert isinstance(error, dict)
            assert error['code'] == 'invalid_request'

            waiting = StdioRunRequest(
                request_id='waiting', agent_id='writer', request=AgentRunRequest(prompt='Queued.')
            )
            incoming.put_nowait(_frame(waiting))
            incoming.put_nowait(_frame(StdioInitializeRequest(request_id='waiting-ready')))
            while (frame := await outgoing.get())['request_id'] != 'waiting-ready':
                received.append(frame)
                assert frame['type'] == 'event'
            received.append(frame)
            assert frame['request_id'] == 'waiting-ready'
            principal = 'other'
            assert calls == 1
            incoming.put_nowait(_frame(StdioCancelRequest(request_id='cancel-waiting', target_request_id='waiting')))
            while (frame := await outgoing.get())['type'] != 'error':
                received.append(frame)
            received.append(frame)
            assert frame['request_id'] == 'waiting'
            error = frame['error']
            assert isinstance(error, dict)
            assert error['code'] == 'run_cancelled'

            incoming.put_nowait(_frame(StdioCancelRequest(request_id='wrong', target_request_id='missing')))
            incoming.put_nowait(_frame(StdioCommandRequest(request_id='queued', command_id='inspect')))
            incoming.put_nowait(_frame(StdioInitializeRequest(request_id='probe')))
            while (frame := await outgoing.get())['request_id'] != 'probe':
                received.append(frame)
                assert frame['type'] == 'event'
            received.append(frame)

            incoming.put_nowait(_frame(StdioCancelRequest(request_id='cancel', target_request_id='first')))
            while (frame := await outgoing.get())['type'] != 'error':
                received.append(frame)
            received.append(frame)
            assert frame['request_id'] == 'first'
            error = frame['error']
            assert isinstance(error, dict)
            assert error['code'] == 'run_cancelled'
            failed = received[-2]['event']
            assert isinstance(failed, dict)
            assert failed['kind'] == 'run_failed'
            previous = [frame['event'] for frame in received[:-2] if frame['type'] == 'event'][-1]
            assert isinstance(previous, dict)
            assert failed['run_id'] == previous['run_id']
            assert failed['conversation_id'] == previous['conversation_id']
            sequence = previous['sequence']
            assert isinstance(sequence, int)
            assert failed['sequence'] == sequence + 1
            frame = await outgoing.get()
            received.append(frame)
            assert frame['type'] == 'command_result'
            assert frame['request_id'] == 'queued'
            assert frame['result'] == 'ordered'

            incoming.put_nowait(_frame(StdioCancelRequest(request_id='stale', target_request_id='first')))
            incoming.put_nowait(_frame(second))
            while (frame := await outgoing.get())['type'] != 'run_result':
                received.append(frame)
            received.append(frame)
            assert frame['request_id'] == 'second'
            result = frame['result']
            assert isinstance(result, dict)
            assert result['output'] == 'Hello server'
            incoming.put_nowait(b'')
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert [_PAYLOAD_ADAPTER.validate_json(line) for line in wire.splitlines()] == received
    assert not any(
        frame['request_id'] in ('wrong', 'cancel', 'stale', 'denied', 'cancel-waiting', 'denied-waiting')
        for frame in received
    )


async def test_stdio_pre_stream_cancellation_preserves_owner_and_releases_request_id() -> None:
    preparing = asyncio.Event()
    released = asyncio.Event()
    registration = await build_registration()
    calls = 0

    async def dependencies(context: RequestContext, authorization: AuthorizationResult) -> AgentDependencies:
        nonlocal calls
        calls += 1
        if calls == 1:
            preparing.set()
            try:
                await asyncio.Event().wait()
            finally:
                released.set()
        return await registration.dependencies(context, authorization)

    async def authorize(_: RequestContext, __: str) -> AuthorizationResult:
        return AuthorizationResult(allowed=True)

    incoming: asyncio.Queue[bytes] = asyncio.Queue()
    outgoing: asyncio.Queue[dict[str, JsonValue]] = asyncio.Queue()

    async def read(_: int) -> bytes:
        return await incoming.get()

    async def write(payload: bytes) -> None:
        outgoing.put_nowait(_PAYLOAD_ADAPTER.validate_json(payload))

    server = create_stdio_server(
        agents=(replace(registration, dependencies=dependencies),),
        authorize=authorize,
    )
    request = StdioRunRequest(request_id='preparing', agent_id='writer', request=AgentRunRequest(prompt='Run.'))
    task = asyncio.create_task(server._serve(reader=read, writer=write))
    try:
        async with asyncio.timeout(5):
            incoming.put_nowait(_frame(request))
            await preparing.wait()
            incoming.put_nowait(_frame(StdioCancelRequest(request_id='cancel', target_request_id='preparing')))
            frame = await outgoing.get()
            assert frame['type'] == 'error'
            assert frame['request_id'] == 'preparing'
            error = frame['error']
            assert isinstance(error, dict)
            assert error['code'] == 'run_cancelled'
            assert released.is_set()
            incoming.put_nowait(_frame(StdioCancelRequest(request_id='stale', target_request_id='preparing')))
            incoming.put_nowait(_frame(request))
            while (frame := await outgoing.get())['type'] == 'event':
                pass
            assert frame['type'] == 'run_result'
            assert frame['request_id'] == 'preparing'
            incoming.put_nowait(b'')
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_stdio_registration_validation() -> None:
    async def command(_: RequestContext, __: AuthorizationResult, arguments: JsonValue) -> JsonValue:
        return arguments

    async def authorize(_: RequestContext, __: str) -> AuthorizationResult:
        return AuthorizationResult(allowed=True)

    with pytest.raises(ValueError, match='command registration id'):
        CommandRegistration(id='bad command', description='Bad.', handler=command)
    with pytest.raises(ValueError, match='description'):
        CommandRegistration(id='valid', description='   ', handler=command)
    with pytest.raises(ValueError, match='unique'):
        create_stdio_server(
            agents=(),
            commands=(
                CommandRegistration(id='same', description='One.', handler=command),
                CommandRegistration(id='same', description='Two.', handler=command),
            ),
            authorize=authorize,
        )


async def _serve_frames(
    server: StdioAgentServer,
    mocker: MockerFixture,
    terminal_id: str,
    requests: Sequence[bytes],
    *,
    ending: bytes = b'',
) -> _WriterMock:
    incoming = iter(requests)
    finished = asyncio.Event()

    async def read(_: int) -> bytes:
        line = next(incoming, None)
        if line is not None:
            return line
        await finished.wait()
        return ending

    async def write(payload: bytes) -> None:
        frame = _PAYLOAD_ADAPTER.validate_json(payload)
        if frame['request_id'] == terminal_id and frame['type'] in ('run_result', 'command_result', 'error'):
            finished.set()

    writer = mocker.AsyncMock(side_effect=write)
    async with asyncio.timeout(3):
        await server._serve(reader=read, writer=writer)
    return writer
