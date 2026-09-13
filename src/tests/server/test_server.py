import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import pytest
from pytest_mock import MockerFixture
from starlette.applications import Starlette

from ovid_core import AgentRunError, AgentRunPolicy, InMemoryConversationStore
from ovid_core.agents import AgentStream
from ovid_core.messages.models import AgentMessage
from ovid_core.runtime import AgentEvent, ConversationId, RunCompletedEvent, RunId
from ovid_core.server import AgentRunRequest, AuthorizationResult, RequestContext, ServerConfig, create_agent_app
from ovid_core.server.errors import _AuthorizationDeniedError, _UnknownAgentError
from ovid_core.server.runtime import _AgentServerRuntime
from tests.server.server_helpers import allow, build_registration, server_client
from tests.support.agent_consumer import AgentDependencies


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
                '/agents/writer/events',
                headers={'Authorization': 'Bearer allowed', 'Origin': 'https://example.com'},
                json={'prompt': 'Write.', 'conversation_id': str(conversation_id)},
            )
            stream = await client.post(
                '/agents/writer/events',
                headers={'Authorization': 'Bearer allowed'},
                json={'prompt': 'Continue.', 'conversation_id': str(conversation_id)},
            )
            preflight = await client.options(
                '/agents/writer/events',
                headers={
                    'Origin': 'https://example.com',
                    'Access-Control-Request-Method': 'POST',
                },
            )
            removed = await client.post('/agents/writer/runs', json={'prompt': 'Write.'})

    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    persisted = await store.load(conversation_id)

    assert lifecycle == ['startup', 'shutdown']
    assert health.json() == {'status': 'ok'}
    assert unavailable.status_code == 503
    assert available.status_code == 200
    assert removed.status_code == 404
    assert events[-1]['output'] == 'Hello server'
    assert [event['kind'] for event in events][-3:] == ['usage_update', 'run_completed', 'run_result']
    assert sum(event['kind'] == 'run_completed' for event in events) == 1
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
        forbidden = await client.post('/agents/writer/events', json={'prompt': 'Write.'})
        authority = await client.post(
            '/agents/writer/events',
            headers={'Authorization': 'Bearer allowed'},
            json={'prompt': 'Write.', 'system_prompt': 'replace'},
        )
        oversized = await client.post(
            '/agents/writer/events',
            headers={'Authorization': 'Bearer allowed'},
            json={'prompt': 'x' * 100},
        )
        invalid = await client.post(
            '/agents/writer/events',
            headers={'Authorization': 'Bearer allowed', 'Content-Type': 'application/json'},
            content=b'{',
        )
        unsupported = await client.post(
            '/agents/writer/events',
            headers={'Authorization': 'Bearer allowed', 'Content-Type': 'text/plain'},
            content='Write.',
        )
        missing = await client.post(
            '/agents/missing/events',
            headers={'Authorization': 'Bearer allowed'},
            json={'prompt': 'Write.'},
        )

    assert '"code":"forbidden"' in forbidden.text
    assert authority.json()['code'] == 'invalid_request'
    assert oversized.status_code == 413
    assert invalid.status_code == 422
    assert unsupported.status_code == 415
    assert '"code":"agent_not_found"' in missing.text
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
    first = asyncio.create_task(_consume(runtime, 'writer', request, context))
    await entered.wait()
    second = asyncio.create_task(_consume(runtime, 'writer', request, context))
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
        await _consume(timeout_runtime, 'writer', request, context)

    with pytest.raises(_UnknownAgentError):
        await _consume(runtime, 'missing', request, context)

    denied_runtime = _AgentServerRuntime(
        agents=(registration,),
        authorize=allow,
        config=ServerConfig(),
        store=None,
    )
    with pytest.raises(_AuthorizationDeniedError):
        await _consume(denied_runtime, 'writer', request, context)

    mocker.patch.object(registration.agent, 'stream', side_effect=AgentRunError('safe failure'))
    app = cast(Starlette, create_agent_app(agents=(registration,), authorize=allow))
    async with server_client(app) as client:
        failed = await client.post(
            '/agents/writer/events',
            headers={'Authorization': 'Bearer allowed'},
            json={'prompt': 'Write.'},
        )

    assert '"code":"agent_run_failed"' in failed.text
    assert '"message":"safe failure"' in failed.text


async def _consume(
    runtime: _AgentServerRuntime,
    agent_id: str,
    request: AgentRunRequest,
    context: RequestContext,
) -> None:
    async with runtime.stream(agent_id, request, context) as stream:
        async for _ in stream:
            pass


async def test_server_delivers_persisted_result_without_optional_completion_event(mocker: MockerFixture) -> None:
    registration = await build_registration()
    original_stream = registration.agent.stream

    @asynccontextmanager
    async def result_only_stream(
        prompt: str,
        *,
        deps: AgentDependencies,
        messages: tuple[AgentMessage, ...],
        conversation_id: ConversationId,
        run_id: RunId,
    ) -> AsyncIterator[AgentStream[str]]:
        async with original_stream(
            prompt,
            deps=deps,
            messages=messages,
            conversation_id=conversation_id,
            run_id=run_id,
        ) as stream:

            async def events() -> AsyncIterator[AgentEvent]:
                async for event in stream:
                    if not isinstance(event, RunCompletedEvent):
                        yield event
                adapted.result = stream.result

            adapted = mocker.MagicMock()
            adapted.__aiter__.side_effect = events
            yield adapted

    mocker.patch.object(registration.agent, 'stream', new=result_only_stream)
    store = InMemoryConversationStore()
    app = create_agent_app(agents=(registration,), authorize=allow, store=store)
    conversation_id = ConversationId.new()
    async with server_client(app) as client:
        response = await client.post(
            '/agents/writer/events',
            headers={'Authorization': 'Bearer allowed', 'Content-Type': 'application/json'},
            content=AgentRunRequest(prompt='Write.', conversation_id=conversation_id).model_dump_json(),
        )
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert events[-1]['kind'] == 'run_result'
    assert events[-1]['output'] == 'Hello server'
    assert not any(event['kind'] == 'run_completed' for event in events)
    assert [message.model_dump(mode='json') for message in await store.load(conversation_id)] == events[-1]['messages']
