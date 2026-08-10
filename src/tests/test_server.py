import asyncio
from typing import cast

import pytest
from pytest_mock import MockerFixture
from starlette.applications import Starlette

from ovid_core.errors import AgentRunError
from ovid_core.persistence import InMemoryConversationStore
from ovid_core.policy import AgentRunPolicy
from ovid_core.runtime.identifiers import ConversationId
from ovid_core.server.app import create_agent_app
from ovid_core.server.contracts import AuthorizationResult, RequestContext
from ovid_core.server.models import AgentRunRequest, AgentRunResponse, ServerConfig
from ovid_core.server.runtime import _AgentServerRuntime, _AuthorizationDeniedError, _UnknownAgentError
from tests.server_helpers import allow, build_registration, server_client


async def test_native_server_runs_streams_persists_authoritative_history_and_lifecycle() -> None:
    registration = await build_registration(policy=AgentRunPolicy(max_concurrency=2, timeout_seconds=5))
    store = InMemoryConversationStore()
    lifecycle: list[str] = []
    ready = False

    async def startup() -> None:
        lifecycle.append('startup')

    async def shutdown() -> None:
        lifecycle.append('shutdown')

    async def readiness() -> bool:
        return ready

    app = cast(
        Starlette,
        create_agent_app(
            agents=(registration,),
            authorize=allow,
            config=ServerConfig(allowed_origins=('https://example.com',)),
            store=store,
            readiness=readiness,
            startup=startup,
            shutdown=shutdown,
        ),
    )
    conversation_id = ConversationId.new()

    async with app.router.lifespan_context(app):
        async with server_client(app) as client:
            health = await client.get('/health')
            unavailable = await client.get('/ready')
            ready = True
            available = await client.get('/ready')
            response = await client.post(
                '/agents/writer/runs',
                headers={'Authorization': 'Bearer allowed', 'Origin': 'https://example.com'},
                json={'prompt': 'Write.', 'conversation_id': str(conversation_id)},
            )
            stream = await client.post(
                '/agents/writer/events',
                headers={'Authorization': 'Bearer allowed'},
                json={'prompt': 'Continue.', 'conversation_id': str(conversation_id)},
            )
            preflight = await client.options(
                '/agents/writer/runs',
                headers={
                    'Origin': 'https://example.com',
                    'Access-Control-Request-Method': 'POST',
                },
            )

    payload = AgentRunResponse.model_validate(response.json())
    persisted = await store.load(conversation_id)

    assert lifecycle == ['startup', 'shutdown']
    assert health.json() == {'status': 'ok'}
    assert unavailable.status_code == 503
    assert available.status_code == 200
    assert payload.output == 'Hello server'
    assert len(persisted) == 4
    assert stream.headers['content-type'].startswith('text/event-stream')
    assert 'event: text_delta' in stream.text
    assert 'event: run_result' in stream.text
    assert '"usage"' in stream.text
    assert preflight.status_code == 200
    assert response.headers['access-control-allow-origin'] == 'https://example.com'


async def test_native_server_rejects_untrusted_invalid_and_oversized_requests() -> None:
    registration = await build_registration()
    app = cast(
        Starlette,
        create_agent_app(
            agents=(registration,),
            authorize=allow,
            config=ServerConfig(max_body_bytes=60),
        ),
    )

    async with server_client(app) as client:
        forbidden = await client.post('/agents/writer/runs', json={'prompt': 'Write.'})
        authority = await client.post(
            '/agents/writer/runs',
            headers={'Authorization': 'Bearer allowed'},
            json={'prompt': 'Write.', 'system_prompt': 'replace'},
        )
        oversized = await client.post(
            '/agents/writer/runs',
            headers={'Authorization': 'Bearer allowed'},
            json={'prompt': 'x' * 100},
        )
        invalid = await client.post(
            '/agents/writer/runs',
            headers={'Authorization': 'Bearer allowed', 'Content-Type': 'application/json'},
            content=b'{',
        )
        unsupported = await client.post(
            '/agents/writer/runs',
            headers={'Authorization': 'Bearer allowed', 'Content-Type': 'text/plain'},
            content='Write.',
        )
        missing = await client.post(
            '/agents/missing/runs',
            headers={'Authorization': 'Bearer allowed'},
            json={'prompt': 'Write.'},
        )

    assert forbidden.status_code == 403
    assert authority.json()['code'] == 'invalid_request'
    assert oversized.status_code == 413
    assert invalid.status_code == 422
    assert unsupported.status_code == 415
    assert missing.status_code == 404
    assert 'replace' not in authority.text


async def test_server_runtime_limits_concurrency_timeout_and_safe_failures(mocker: MockerFixture) -> None:
    registration = await build_registration()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def blocking_authorize(context: RequestContext, agent_id: str) -> AuthorizationResult:
        nonlocal calls
        del context, agent_id
        calls += 1
        entered.set()
        await release.wait()

        return AuthorizationResult(allowed=True)

    runtime = _AgentServerRuntime(
        agents=(registration,),
        authorize=blocking_authorize,
        config=ServerConfig(max_concurrency=1, request_timeout_seconds=1),
        store=None,
    )

    context = RequestContext(method='POST', path='/run', request_id='request')
    request = AgentRunRequest(prompt='Write.')
    first = asyncio.create_task(runtime.run('writer', request, context))
    await entered.wait()
    second = asyncio.create_task(runtime.run('writer', request, context))
    await asyncio.sleep(0)

    assert calls == 1

    release.set()
    await first
    await second

    async def slow_authorize(context: RequestContext, agent_id: str) -> AuthorizationResult:
        del context, agent_id
        await asyncio.sleep(1)

        return AuthorizationResult(allowed=True)

    timeout_runtime = _AgentServerRuntime(
        agents=(registration,),
        authorize=slow_authorize,
        config=ServerConfig(request_timeout_seconds=0.001),
        store=None,
    )
    with pytest.raises(TimeoutError):
        await timeout_runtime.run('writer', request, context)

    with pytest.raises(_UnknownAgentError):
        await runtime.run('missing', request, context)

    denied_runtime = _AgentServerRuntime(
        agents=(registration,),
        authorize=allow,
        config=ServerConfig(),
        store=None,
    )
    with pytest.raises(_AuthorizationDeniedError):
        await denied_runtime.run('writer', request, context)

    mocker.patch.object(registration.agent, 'run', side_effect=AgentRunError('safe failure'))
    app = cast(Starlette, create_agent_app(agents=(registration,), authorize=allow))
    async with server_client(app) as client:
        failed = await client.post(
            '/agents/writer/runs',
            headers={'Authorization': 'Bearer allowed'},
            json={'prompt': 'Write.'},
        )

    assert failed.status_code == 502
    assert failed.json()['message'] == 'safe failure'
