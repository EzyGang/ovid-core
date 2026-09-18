import json

import httpx
import pytest
from pydantic import SecretStr
from pydantic_ai.models.test import TestModel
from pytest_mock import MockerFixture

from ovid_core import AgentDefinition, AgentFactory, ModelResolutionError
from ovid_core.config import ModelConfig, OvidConfig
from ovid_core.errors import CredentialError
from ovid_core.routing import ModelHandle, ModelRouteRef, ModelRuntime
from ovid_core.tools import ToolExecutionContext, ToolResult
from tests.routing.fixtures import model_reply
from tests.support.agent_consumer import AddArguments, AddTool, AgentDependencies, ConsumerToolset
from tests.support.agent_helpers import RuntimeFactory, UnsupportedRuntime


@pytest.mark.parametrize('failure', [CredentialError('unavailable'), RuntimeError('resolver-secret')])
async def test_route_resolution_failure_never_uses_stale_or_other_candidate(
    mocker: MockerFixture, failure: Exception
) -> None:
    requests: list[httpx.Request] = []
    unavailable = False

    async def resolve(model_id: str, provider: str) -> SecretStr:
        if unavailable and model_id == 'secondary':
            raise failure
        return SecretStr(f'{model_id}-key')

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return model_reply()

    mocker.patch(
        'pydantic_ai.providers.openai.create_async_http_client',
        side_effect=lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    config = OvidConfig.model_validate(
        {
            'models': {name: {'provider': 'openai-chat', 'model': 'gpt-4o'} for name in ('primary', 'secondary')},
            'routes': {'answer': {'models': ['primary', 'secondary']}},
        }
    )
    agent = await AgentFactory(config=config, provider_api_key=resolve).build(
        AgentDefinition(model=ModelRouteRef(name='answer'), deps_type=type(None), output_type=str)
    )
    assert (await agent.run('initial', deps=None)).output == 'done'
    unavailable = True

    with pytest.raises(CredentialError if isinstance(failure, CredentialError) else ModelResolutionError) as failed:
        await agent.run('must not send', deps=None)

    assert 'resolver-secret' not in repr(failed.value)
    assert failed.value.__cause__ is None
    assert [request.headers['authorization'] for request in requests] == ['Bearer primary-key']


async def test_dynamic_fallback_preserves_order_and_settings_between_tool_steps(mocker: MockerFixture) -> None:
    requests: list[httpx.Request] = []
    version = 'old'

    async def resolve(model_id: str, provider: str) -> SecretStr:
        return SecretStr(f'{model_id}-{version}')

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers['authorization'].startswith('Bearer primary-'):
            return httpx.Response(503, headers={'x-should-retry': 'false'}, json={'error': {'message': 'unavailable'}})
        return model_reply(tool_value=1 if version == 'old' else None)

    mocker.patch(
        'pydantic_ai.providers.openai.create_async_http_client',
        side_effect=lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    tool = AddTool()

    async def execute(context: ToolExecutionContext[AgentDependencies], arguments: AddArguments) -> ToolResult:
        nonlocal version
        version = 'new'
        return ToolResult(content=1)

    mocker.patch.object(tool, 'execute', side_effect=execute)
    config = OvidConfig.model_validate(
        {
            'models': {
                'primary': {'provider': 'openai-chat', 'model': 'gpt-4o', 'settings': {'temperature': 0.2}},
                'secondary': {'provider': 'openai-chat', 'model': 'gpt-4o', 'settings': {'temperature': 0.7}},
            },
            'routes': {'answer': {'models': ['primary', 'secondary']}},
        }
    )
    agent = await AgentFactory(config=config, provider_api_key=resolve).build(
        AgentDefinition(
            model=ModelRouteRef(name='answer'),
            deps_type=AgentDependencies,
            output_type=str,
            toolsets=(ConsumerToolset((tool,)),),
        )
    )

    assert (await agent.run('Use the tool.', deps=AgentDependencies(prefix='fallback'))).output == 'done'
    assert [request.headers['authorization'] for request in requests] == [
        'Bearer primary-old',
        'Bearer secondary-old',
        'Bearer primary-new',
        'Bearer secondary-new',
    ]
    assert [json.loads(request.content)['temperature'] for request in requests] == [0.2, 0.7, 0.2, 0.7]


@pytest.mark.parametrize('mode', ['static', 'dynamic', 'error'])
async def test_model_selection_rejects_invalid_runtimes_and_redacts_failures(mocker: MockerFixture, mode: str) -> None:
    factory = RuntimeFactory(
        {
            'first': UnsupportedRuntime() if mode == 'static' else TestModel(),
            'second': TestModel(),
        }
    )
    build = factory.build

    async def resolve() -> ModelRuntime:
        if mode == 'error':
            raise RuntimeError('credential-secret')
        return UnsupportedRuntime()

    async def construct(*, model_id: str, config: ModelConfig) -> ModelHandle:
        handle = await build(model_id=model_id, config=config)
        handle.resolve = resolve
        return handle

    if mode != 'static':
        mocker.patch.object(factory, 'build', side_effect=construct)
    config = OvidConfig.model_validate(
        {
            'models': {name: {'provider': 'test', 'model': 'test'} for name in ('first', 'second')},
            'routes': {'answer': {'models': ['first', 'second']}},
        }
    )
    with pytest.raises(ModelResolutionError) as failed:
        agent = await AgentFactory(config=config, model_factory=factory).build(
            AgentDefinition(model=ModelRouteRef(name='answer'), deps_type=type(None), output_type=str)
        )
        await agent.run('Must not execute an invalid model.', deps=None)
    assert 'credential-secret' not in str(failed.value)
    assert failed.value.__cause__ is None
