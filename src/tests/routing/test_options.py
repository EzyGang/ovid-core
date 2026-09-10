import inspect
from typing import Any

from pydantic_ai.providers import Provider, infer_provider_class
from pytest_mock import MockerFixture

from ovid_core.adapters.pydantic_ai import (
    available_api_key_model_options,
    available_api_key_models,
    available_model_options,
    known_models,
)
from ovid_core.routing import KnownModel


def test_known_models_split_provider_identifiers(mocker: MockerFixture) -> None:
    mocker.patch(
        'ovid_core.adapters.pydantic_ai.models.known_model_names',
        return_value=('openai:gpt-5.6-sol', 'anthropic:claude-sonnet-5', 'test'),
    )

    assert known_models() == (
        KnownModel(provider='openai', model='gpt-5.6-sol'),
        KnownModel(provider='anthropic', model='claude-sonnet-5'),
        KnownModel(provider='test', model='test'),
    )


def test_available_options_are_grouped_sorted_and_versioned(mocker: MockerFixture) -> None:
    mocker.patch(
        'ovid_core.adapters.pydantic_ai.models.known_model_names',
        return_value=('openai:gpt-z', 'openai:gpt-a', 'test'),
    )

    options = available_model_options(
        additional_models=(
            KnownModel(provider='codex-subscription', model='gpt-codex'),
            KnownModel(provider='openai', model='gpt-a'),
        )
    )

    assert options.schema_version == 1
    assert tuple(provider.value for provider in options.providers) == ('codex-subscription', 'openai')
    assert tuple(model.value for model in options.providers[1].models) == ('gpt-a', 'gpt-z')
    assert tuple(option.value for option in options.reasoning_efforts) == (
        'off',
        'minimal',
        'low',
        'medium',
        'high',
        'xhigh',
    )
    assert options.model_dump(mode='json')['providers'][0]['models'][0]['value'] == 'gpt-codex'


def test_available_api_key_options_cover_the_upstream_catalog(mocker: MockerFixture) -> None:
    models = (
        KnownModel(provider='openai', model='gpt-a'),
        KnownModel(provider='anthropic', model='claude'),
        KnownModel(provider='test', model='test'),
    )
    mocker.patch('ovid_core.adapters.pydantic_ai.models.known_models', return_value=models)

    assert available_api_key_models() == models[:2]
    options = available_api_key_model_options()
    assert [provider.value for provider in options.providers] == ['anthropic', 'openai']


def test_api_key_options_exclude_unavailable_and_keyless_providers(mocker: MockerFixture) -> None:
    models = (
        KnownModel(provider='openai', model='gpt-a'),
        KnownModel(provider='missing-sdk', model='model'),
        KnownModel(provider='keyless', model='model'),
        KnownModel(provider='unknown', model='model'),
    )
    keyless = mocker.Mock(__signature__=inspect.Signature())

    def resolve_provider(provider: str) -> type[Provider[Any]]:
        if provider == 'missing-sdk':
            raise ImportError('Optional provider SDK is unavailable')
        if provider == 'unknown':
            raise ValueError('Unknown provider')
        if provider == 'keyless':
            return keyless
        return infer_provider_class(provider)

    mocker.patch('ovid_core.adapters.pydantic_ai.models.known_models', return_value=models)
    mocker.patch('ovid_core.adapters.pydantic_ai.models.infer_provider_class', side_effect=resolve_provider)

    options = available_api_key_model_options()

    assert tuple(provider.value for provider in options.providers) == ('openai',)
    assert tuple(model.value for model in options.providers[0].models) == ('gpt-a',)
