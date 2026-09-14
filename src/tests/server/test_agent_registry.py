import asyncio
from dataclasses import replace

import pytest
from pydantic import JsonValue, TypeAdapter
from pytest_mock import MockerFixture

from ovid_core.server import (
    AgentRegistry,
    AgentRunRequest,
    AuthorizationResult,
    CommandRegistration,
    RequestContext,
    StdioCommandRequest,
    StdioCommandResultResponse,
    StdioInitializedResponse,
    StdioInitializeRequest,
    StdioResponse,
    StdioRunRequest,
    StdioRunResultResponse,
    create_agent_app,
    create_stdio_server,
)
from tests.server.server_helpers import allow, build_registration, server_client


async def test_registry_rejects_duplicate_batches_without_partial_registration() -> None:
    registration = await build_registration()
    registry = AgentRegistry((registration,))
    other = replace(registration, id='other')
    with pytest.raises(ValueError):
        registry.register((other, registration))
    assert tuple(registry) == (registration,)
    assert len(registry) == 1
    assert registry.get('other') is None
    assert registry.get(registration.id) is registration
    with pytest.raises(ValueError):
        registry.register((other, other))
    assert tuple(registry) == (registration,)
    assert registry.get('other') is None
    registry.register((other,))
    assert registry[:] == [registration, other]
    assert len(registry) == 2
    assert registry.get('other') is other


async def test_http_resolves_agents_registered_after_server_creation() -> None:
    registry = AgentRegistry()
    app = create_agent_app(agents=registry, authorize=allow)
    async with server_client(app) as client:
        before = await client.post('/agents/writer/events', json={'prompt': 'Write.'})
        assert 'agent_not_found' in before.text
        registry.register((await build_registration(),))
        denied = await client.post('/agents/writer/events', json={'prompt': 'Write.'})
        assert 'forbidden' in denied.text
        response = await client.post(
            '/agents/writer/events',
            json={'prompt': 'Write.'},
            headers={'Authorization': 'Bearer allowed'},
        )
    assert '"output":"Hello server"' in response.text
    assert 'event: run_result' in response.text


async def test_stdio_discovers_and_runs_agent_registered_by_setup_command(mocker: MockerFixture) -> None:
    registry = AgentRegistry()
    registration = await build_registration()
    requests: asyncio.Queue[bytes] = asyncio.Queue()
    responses: list[StdioResponse] = []
    adapter = TypeAdapter(StdioResponse)
    requests.put_nowait(StdioInitializeRequest(request_id='before').model_dump_json().encode())

    async def authorize(context: RequestContext, resource_id: str) -> AuthorizationResult:
        return AuthorizationResult(allowed=True, principal='local')

    async def activate(context: RequestContext, authorization: AuthorizationResult, arguments: JsonValue) -> JsonValue:
        registry.register((registration,))
        return None

    async def read(limit: int) -> bytes:
        return await requests.get()

    async def write(payload: bytes) -> None:
        response = adapter.validate_json(payload)
        responses.append(response)
        if isinstance(response, StdioInitializedResponse):
            request = (
                StdioCommandRequest(request_id='activate', command_id='activate')
                if response.request_id == 'before'
                else StdioRunRequest(request_id='run', agent_id='writer', request=AgentRunRequest(prompt='Write.'))
            )
            requests.put_nowait(request.model_dump_json().encode())
        elif isinstance(response, StdioCommandResultResponse):
            requests.put_nowait(StdioInitializeRequest(request_id='after').model_dump_json().encode())
        elif isinstance(response, StdioRunResultResponse):
            requests.put_nowait(b'')

    mocker.patch('ovid_core.server.stdio._read_stdin', side_effect=read)
    mocker.patch('ovid_core.server.stdio._write_stdout', side_effect=write)
    server = create_stdio_server(
        agents=registry,
        authorize=authorize,
        commands=(CommandRegistration(id='activate', description='Activate agent.', handler=activate),),
    )
    async with asyncio.timeout(3):
        await server.run()
    discoveries = [response for response in responses if isinstance(response, StdioInitializedResponse)]
    assert discoveries[0].agents == ()
    assert [agent.id for agent in discoveries[1].agents] == ['writer']
    results = [response.result.output for response in responses if isinstance(response, StdioRunResultResponse)]
    assert results == ['Hello server']
