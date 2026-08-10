import asyncio
import json
from typing import Protocol, cast

import pytest
from pydantic import JsonValue, TypeAdapter
from pytest_mock import MockerFixture

from ovid_core.errors import AgentRunError, PersistenceError
from ovid_core.persistence import InMemoryConversationStore
from ovid_core.runtime.identifiers import ConversationId
from ovid_core.server.contracts import AuthorizationResult, CommandRegistration, RequestContext
from ovid_core.server.models import AgentRunRequest, ServerConfig
from ovid_core.server.stdio import create_stdio_server
from ovid_core.server.stdio_models import StdioCommandRequest, StdioInitializeRequest, StdioRunRequest
from tests.server_helpers import build_registration


class _MockCall(Protocol):
    @property
    def args(self) -> tuple[bytes, ...]: ...


class _WriterMock(Protocol):
    @property
    def await_args_list(self) -> list[_MockCall]: ...


_PAYLOAD_ADAPTER = TypeAdapter(dict[str, JsonValue])


def _frame(request: StdioInitializeRequest | StdioRunRequest | StdioCommandRequest) -> bytes:
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
    reader = mocker.AsyncMock(
        side_effect=(
            _frame(StdioInitializeRequest(request_id='initialize')),
            _frame(
                StdioRunRequest(
                    request_id='run',
                    agent_id='writer',
                    request=AgentRunRequest(prompt='Write.', conversation_id=conversation_id),
                )
            ),
            _frame(StdioCommandRequest(request_id='command', command_id='inspect', arguments={'value': 3})),
            b'',
        )
    )
    writer = mocker.AsyncMock()

    await server._serve(reader=reader, writer=writer)

    payloads = _payloads(writer)
    initialized = payloads[0]
    run_payloads = [payload for payload in payloads if payload['request_id'] == 'run']
    command_result = payloads[-1]

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
        b'',
    ]
    reader = mocker.AsyncMock(side_effect=requests)
    writer = mocker.AsyncMock()

    await server._serve(reader=reader, writer=writer)

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
    reader = mocker.AsyncMock(
        side_effect=(
            _frame(StdioRunRequest(request_id='run', agent_id='writer', request=AgentRunRequest(prompt='Run.'))),
            b'',
        )
    )
    writer = mocker.AsyncMock()

    await server._serve(reader=reader, writer=writer)

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
