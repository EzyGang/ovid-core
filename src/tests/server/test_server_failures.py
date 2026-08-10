import asyncio
import sys
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import cast

import pytest
from pydantic import ValidationError
from pytest_mock import MockerFixture
from starlette.applications import Starlette

from ovid_core import PersistenceError, ServerConstructionError
from ovid_core.server import AuthorizationResult, RequestContext, ServerConfig, create_agent_app, serve
from ovid_core.server.runtime import _AgentServerRuntime
from tests.server.server_helpers import allow, build_registration, server_client


async def test_server_contract_validation_and_optional_dependency_failures(mocker: MockerFixture) -> None:
    registration = await build_registration()

    with pytest.raises(ValueError, match='at least one'):
        _AgentServerRuntime(agents=(), authorize=allow, config=ServerConfig(), store=None)
    with pytest.raises(ValueError, match='unique'):
        _AgentServerRuntime(agents=(registration, registration), authorize=allow, config=ServerConfig(), store=None)
    with pytest.raises(ValueError, match='letters'):
        type(registration)(
            id='bad/id',
            description='bad',
            agent=registration.agent,
            dependencies=registration.dependencies,
        )
    with pytest.raises(ValueError, match='empty'):
        type(registration)(
            id='valid',
            description='',
            agent=registration.agent,
            dependencies=registration.dependencies,
        )
    with pytest.raises(ValidationError):
        ServerConfig(port=0)

    context = RequestContext(
        method='GET',
        path='/health',
        headers=(('Authorization', 'secret'), ('Traceparent', 'trace')),
        client_host='127.0.0.1',
        request_id='request',
    )
    assert context.header('traceparent') == 'trace'
    assert 'secret' not in repr(context)

    fake_uvicorn = mocker.Mock()
    mocker.patch.dict(sys.modules, {'uvicorn': fake_uvicorn})
    app = create_agent_app(agents=(registration,), authorize=allow)
    serve(app, config=ServerConfig(host='0.0.0.0', port=9000, shutdown_grace_seconds=3))

    fake_uvicorn.run.assert_called_once()
    mocker.stopall()

    mocker.patch.dict(sys.modules, {'uvicorn': None})
    with pytest.raises(ServerConstructionError, match='launcher'):
        serve(create_agent_app(agents=(registration,), authorize=allow))

    mocker.stopall()
    mocker.patch.dict(sys.modules, {'ovid_core.adapters.starlette.app': None})
    with pytest.raises(ServerConstructionError, match='server extra'):
        create_agent_app(agents=(registration,), authorize=allow)


async def test_server_masks_persistence_failures(mocker: MockerFixture) -> None:
    registration = await build_registration()
    store = mocker.Mock()
    store.load = mocker.AsyncMock(side_effect=PersistenceError('database secret'))
    store.append = mocker.AsyncMock()
    app = cast(Starlette, create_agent_app(agents=(registration,), authorize=allow, store=store))

    async with server_client(app) as client:
        persistence = await client.post(
            '/agents/writer/runs',
            headers={'Authorization': 'Bearer allowed'},
            json={'prompt': 'Write.'},
        )

    assert persistence.status_code == 500
    assert persistence.json()['message'] == 'Server operation failed'
    assert 'database secret' not in persistence.text


async def test_server_bounds_streamed_bodies_and_normalizes_stream_timeout_and_internal_errors() -> None:
    registration = await build_registration()
    app = cast(
        Starlette,
        create_agent_app(
            agents=(registration,),
            authorize=allow,
            config=ServerConfig(max_body_bytes=30),
        ),
    )

    async def oversized_body() -> AsyncIterator[bytes]:
        yield b'{' + (b'x' * 20)
        yield b'x' * 20

    async with app.router.lifespan_context(app):
        async with server_client(app) as client:
            streamed = await client.post(
                '/agents/writer/runs',
                headers={'Authorization': 'Bearer allowed', 'Content-Type': 'application/json'},
                content=oversized_body(),
            )
            invalid_length = await client.post(
                '/agents/writer/runs',
                headers={
                    'Authorization': 'Bearer allowed',
                    'Content-Type': 'application/json',
                    'Content-Length': 'invalid',
                },
                content=b'{}',
            )
            invalid_stream = await client.post(
                '/agents/writer/events',
                headers={'Authorization': 'Bearer allowed', 'Content-Type': 'application/json'},
                content=b'{',
            )
            missing_stream = await client.post(
                '/agents/missing/events',
                headers={'Authorization': 'Bearer allowed'},
                json={'prompt': 'Write.'},
            )

    assert streamed.status_code == 413
    assert invalid_length.status_code == 422
    assert invalid_stream.status_code == 422
    assert 'event: server_error' in missing_stream.text

    async def slow_body() -> AsyncIterator[bytes]:
        await asyncio.sleep(1)
        yield b'{"prompt":"Write."}'

    body_timeout_app = cast(
        Starlette,
        create_agent_app(
            agents=(registration,),
            authorize=allow,
            config=ServerConfig(request_timeout_seconds=0.001),
        ),
    )
    async with server_client(body_timeout_app) as client:
        body_timeout = await client.post(
            '/agents/writer/runs',
            headers={'Authorization': 'Bearer allowed', 'Content-Type': 'application/json'},
            content=slow_body(),
        )

    assert body_timeout.status_code == 504

    async def slow_authorize(context: RequestContext, agent_id: str) -> AuthorizationResult:
        del context, agent_id
        await asyncio.sleep(1)

        return AuthorizationResult(allowed=True)

    timeout_app = cast(
        Starlette,
        create_agent_app(
            agents=(registration,),
            authorize=slow_authorize,
            config=ServerConfig(request_timeout_seconds=0.001),
        ),
    )
    async with server_client(timeout_app) as client:
        timeout = await client.post('/agents/writer/runs', json={'prompt': 'Write.'})

    async def broken_dependencies(context: RequestContext, authorization: AuthorizationResult) -> None:
        del context, authorization
        raise ValueError('private dependency failure')

    broken = replace(registration, dependencies=broken_dependencies)
    broken_app = cast(Starlette, create_agent_app(agents=(broken,), authorize=allow))
    async with server_client(broken_app) as client:
        internal = await client.post(
            '/agents/writer/runs',
            headers={'Authorization': 'Bearer allowed'},
            json={'prompt': 'Write.'},
        )

    assert timeout.status_code == 504
    assert internal.status_code == 500
    assert 'private dependency failure' not in internal.text
