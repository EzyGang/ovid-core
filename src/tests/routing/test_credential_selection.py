import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from pytest_mock import MockerFixture

from ovid_core import AgentDefinition, AgentFactory
from ovid_core.config import ModelConfig, OvidConfig
from ovid_core.routing import ModelRef
from ovid_core.tools import ToolExecutionContext, ToolResult
from tests.routing.fixtures import model_reply
from tests.support.agent_consumer import AddArguments, AddTool, AgentDependencies, ConsumerToolset


async def test_tool_steps_reuse_unchanged_credentials_and_select_changed_key(mocker: MockerFixture) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return model_reply(tool_value=len(requests) if len(requests) < 3 else None)

    mocker.patch(
        'pydantic_ai.providers.openai.create_async_http_client',
        side_effect=lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    resolver = mocker.AsyncMock(return_value=SecretStr('old-key'))
    tool = AddTool()

    async def execute(context: ToolExecutionContext[AgentDependencies], arguments: AddArguments) -> ToolResult:
        if arguments.left == 2:
            resolver.return_value = SecretStr('new-key')
        return ToolResult(content=arguments.left)

    mocker.patch.object(tool, 'execute', side_effect=execute)
    agent = await AgentFactory(
        config=OvidConfig(models={'primary': ModelConfig(provider='openai-chat', model='gpt-4o')}),
        provider_api_key=resolver,
    ).build(
        AgentDefinition(
            model=ModelRef(name='primary'),
            deps_type=AgentDependencies,
            output_type=str,
            toolsets=(ConsumerToolset((tool,)),),
        )
    )

    result = await agent.run('Use the tools.', deps=AgentDependencies(prefix='rotation'))

    assert result.output == 'done'
    assert [request.headers['authorization'] for request in requests] == [
        'Bearer old-key',
        'Bearer old-key',
        'Bearer new-key',
    ]
    assert [request.headers.get('cookie') for request in requests] == [None, 'session=retained', None]


@pytest.mark.parametrize('provider', ['openai-chat', 'gateway/openai-chat'])
async def test_active_stream_survives_concurrent_fresh_key_run(mocker: MockerFixture, provider: str) -> None:
    requests: list[httpx.Request] = []
    clients: list[httpx.AsyncClient] = []
    started, release = asyncio.Event(), asyncio.Event()
    mocker.patch.dict('os.environ', {'PYDANTIC_AI_GATEWAY_BASE_URL': 'https://gateway.example.test/proxy'})

    async def chunks() -> AsyncIterator[bytes]:
        chunk: dict[str, Any] = {
            'id': 'stream',
            'object': 'chat.completion.chunk',
            'created': 0,
            'model': 'gpt-4o',
            'choices': [{'index': 0, 'delta': {'content': 'old '}, 'finish_reason': None}],
        }
        yield f'data: {json.dumps(chunk)}\n\n'.encode()
        started.set()
        await release.wait()
        assert not clients[0].is_closed
        chunk['choices'] = [{'index': 0, 'delta': {'content': 'stream'}, 'finish_reason': 'stop'}]
        yield f'data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n'.encode()

    body = mocker.Mock(spec=httpx.AsyncByteStream)
    body.__aiter__ = mocker.Mock(side_effect=chunks)
    body.aclose = mocker.AsyncMock()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if json.loads(request.content).get('stream'):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=body)
        return model_reply('new reply')

    def client() -> httpx.AsyncClient:
        created = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        clients.append(created)
        return created

    mocker.patch('pydantic_ai.providers.openai.create_async_http_client', side_effect=client)
    mocker.patch('pydantic_ai.providers.gateway.create_async_http_client', side_effect=client)
    resolver = mocker.AsyncMock(return_value=SecretStr('old-key'))
    agent = await AgentFactory(
        config=OvidConfig(
            models={'primary': ModelConfig(provider=provider, model='gpt-4o', settings={'temperature': 0.2})}
        ),
        provider_api_key=resolver,
    ).build(AgentDefinition(model=ModelRef(name='primary'), deps_type=type(None), output_type=str))

    async def consume() -> str:
        async with agent.stream('stream', deps=None) as stream:
            async for _ in stream:
                pass
            return stream.result.output

    async with asyncio.TaskGroup() as tasks:
        old = tasks.create_task(consume())
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            resolver.return_value = SecretStr('new-key')
            assert (await agent.run('fresh', deps=None)).output == 'new reply'
        finally:
            release.set()
    assert old.result() == 'old stream'
    assert [request.headers['authorization'] for request in requests] == ['Bearer old-key', 'Bearer new-key']
    assert all(json.loads(request.content)['temperature'] == 0.2 for request in requests)


async def test_rotation_shares_concurrency_limit_with_active_model(mocker: MockerFixture) -> None:
    requests: list[httpx.Request] = []
    started, release, fresh_resolved = asyncio.Event(), asyncio.Event(), asyncio.Event()
    key = 'old-key'

    async def resolve(model_id: str, provider: str) -> SecretStr:
        if key == 'new-key':
            fresh_resolved.set()
        return SecretStr(key)

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers['authorization'] == 'Bearer old-key':
            started.set()
            await release.wait()
        return model_reply()

    mocker.patch(
        'pydantic_ai.providers.openai.create_async_http_client',
        side_effect=lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    agent = await AgentFactory(
        config=OvidConfig(models={'primary': ModelConfig(provider='openai-chat', model='gpt-4o', concurrency_limit=1)}),
        provider_api_key=resolve,
    ).build(AgentDefinition(model=ModelRef(name='primary'), deps_type=type(None), output_type=str))

    async with asyncio.TaskGroup() as tasks:
        old = tasks.create_task(agent.run('old', deps=None))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            key = 'new-key'
            fresh = tasks.create_task(agent.run('fresh', deps=None))
            await asyncio.wait_for(fresh_resolved.wait(), timeout=5)
            done, _ = await asyncio.wait((fresh,), timeout=0.05)
            assert not done
            assert [request.headers['authorization'] for request in requests] == ['Bearer old-key']
        finally:
            release.set()
    assert old.result().output == fresh.result().output == 'done'
    assert [request.headers['authorization'] for request in requests] == ['Bearer old-key', 'Bearer new-key']
