from typing import cast

import pytest
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.concurrency import ConcurrencyLimitedModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers.openai import OpenAIProvider
from pytest_mock import MockerFixture

from ovid_core import DefaultModelFactory, ModelResolutionError
from ovid_core.adapters.pydantic_ai import known_models
from ovid_core.config import ModelConfig, OvidConfig
from ovid_core.errors import CredentialError
from ovid_core.routing import ModelRef, ModelRouter


@pytest.mark.asyncio
async def test_generic_pydantic_factory_applies_settings_concurrency_and_capabilities() -> None:
    config = OvidConfig.model_validate(
        {
            'models': {
                'configured': {
                    'provider': 'test',
                    'model': 'test',
                    'settings': {'temperature': 0.2},
                    'concurrency_limit': 2,
                }
            }
        },
    )
    factory = DefaultModelFactory()
    router = ModelRouter(config=config, factory=factory)

    resolved = await router.resolve(ModelRef(name='configured'))
    upstream = await Agent(cast(Model, resolved.handle._runtime)).run('hello')
    plain = await factory.build(model_id='plain', config=ModelConfig(provider='test', model='test'))

    assert isinstance(resolved.handle._runtime, ConcurrencyLimitedModel)
    assert resolved.handle._runtime.wrapped.settings == {'temperature': 0.2}
    assert resolved.handle.capabilities.tools
    assert upstream.output == 'success (no tool calls)'
    assert isinstance(plain._runtime, TestModel)


@pytest.mark.asyncio
async def test_default_factory_discovers_context_window_from_model_metadata(mocker: MockerFixture) -> None:
    model = OpenAIResponsesModel('gpt-5.4', provider=OpenAIProvider(api_key='test-key'))
    mocker.patch('ovid_core.adapters.pydantic_ai.models.infer_model', return_value=model)

    async with model:
        handle = await DefaultModelFactory().build(
            model_id='primary',
            config=ModelConfig(provider='openai', model='gpt-5.4'),
        )

    assert handle.context_window == 1_050_000


@pytest.mark.asyncio
async def test_context_discovery_falls_back_from_custom_url_to_provider(mocker: MockerFixture) -> None:
    provider = OpenAIProvider(base_url='https://models.example.test/v1', api_key='test-key')
    model = OpenAIResponsesModel('gpt-5.4', provider=provider)
    mocker.patch('ovid_core.adapters.pydantic_ai.models.infer_model', return_value=model)

    async with model:
        handle = await DefaultModelFactory().build(
            model_id='primary',
            config=ModelConfig(provider='openai', model='gpt-5.4'),
        )

    assert handle.context_window == 1_050_000


@pytest.mark.asyncio
async def test_application_gateway_key_preserves_gateway_endpoint(mocker: MockerFixture) -> None:
    mocker.patch.dict('os.environ', {'PYDANTIC_AI_GATEWAY_BASE_URL': 'https://gateway.example.test/proxy'})

    async def provider_api_key(model_id: str, provider: str) -> SecretStr:
        return SecretStr('synthetic-gateway-key')

    handle = await DefaultModelFactory(provider_api_key=provider_api_key).build(
        model_id='gateway',
        config=ModelConfig(provider='gateway/openai', model='gpt-4o'),
    )
    model = cast(Model, handle._runtime)
    async with model:
        assert str(model.base_url) == 'https://gateway.example.test/proxy/openai/'


async def test_resolver_none_preserves_provider_defaults(mocker: MockerFixture) -> None:
    mocker.patch.dict('os.environ', {'OPENAI_API_KEY': 'environment-key'})
    resolver = mocker.AsyncMock(return_value=None)
    handle = await DefaultModelFactory(provider_api_key=resolver).build(
        model_id='primary',
        config=ModelConfig(provider='openai-chat', model='gpt-4o'),
    )
    model = cast(Model, handle._runtime)
    async with model:
        assert isinstance(model.provider, OpenAIProvider)
        assert model.provider.client.api_key == 'environment-key'

    handle = await DefaultModelFactory(provider_api_key=resolver).build(
        model_id='test',
        config=ModelConfig(provider='test', model='test'),
    )
    assert (await Agent(cast(Model, handle._runtime)).run('hello')).output == 'success (no tool calls)'
    with pytest.raises(ModelResolutionError):
        await DefaultModelFactory(provider_api_key=resolver).build(
            model_id='unknown',
            config=ModelConfig(provider='unknown', model='unknown'),
        )


async def test_model_construction_preserves_application_credential_failure() -> None:
    async def unavailable(_model_id: str, _provider: str) -> SecretStr:
        raise CredentialError('Provider credentials are unavailable')

    with pytest.raises(CredentialError):
        await DefaultModelFactory(provider_api_key=unavailable).build(
            model_id='primary', config=ModelConfig(provider='openai', model='gpt-4o')
        )


@pytest.mark.asyncio
async def test_known_catalog_and_generic_construction_errors_are_safe() -> None:
    catalog = known_models()
    factory = DefaultModelFactory()

    assert catalog
    assert all(model.provider and model.model for model in catalog)
    assert any(model.provider == 'openai' for model in catalog)
    with pytest.raises(ModelResolutionError) as captured:
        await factory.build(
            model_id='broken',
            config=ModelConfig(provider='unknown', model='model', settings={'api_key': 'secret-value'}),
        )

    assert 'secret-value' not in repr(captured.value)
    assert captured.value.__cause__ is None
