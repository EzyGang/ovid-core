from collections.abc import Awaitable, Callable
from typing import cast

import pytest
from pydantic import SecretStr
from pytest_mock import MockerFixture

from ovid_core.adapters.pydantic_ai import CodexSubscriptionModelFactory
from ovid_core.authentication import (
    APIKeyAuthenticationFlow,
    ProviderDefinition,
    StoredAPIKeyBinding,
    default_provider_registry,
)
from ovid_core.codex import CodexAuth, MemoryCodexTokenStore
from ovid_core.routing import KnownModel, ModelProviderOption, SelectionOption, model_selection_options


def test_default_registry_derives_pydantic_providers_and_applies_overrides(
    mocker: MockerFixture,
) -> None:
    options = model_selection_options(
        models=(
            KnownModel(provider='openai', model='gpt'),
            KnownModel(provider='cerebras', model='model'),
        )
    )
    mocker.patch(
        'ovid_core.authentication.providers.available_api_key_model_options',
        return_value=options,
    )
    stores: list[str] = []

    def api_key_store(provider: str) -> MemoryStore[SecretStr]:
        stores.append(provider)
        return MemoryStore()

    codex_auth = cast(CodexAuth, mocker.Mock())
    codex_store = MemoryCodexTokenStore()
    http_client = mocker.Mock()
    model_factory = cast(CodexSubscriptionModelFactory, mocker.Mock())
    registry = default_provider_registry(
        api_key_store=api_key_store,
        codex_auth=codex_auth,
        codex_store=codex_store,
        http_client=http_client,
        model_factory=model_factory,
        excluded_provider_ids={'cerebras'},
    )
    assert [definition.id for definition in registry.definitions] == ['codex-subscription', 'openai']
    assert stores == ['openai']
    without_codex = default_provider_registry(
        api_key_store=api_key_store,
        codex_auth=codex_auth,
        codex_store=codex_store,
        http_client=http_client,
        model_factory=model_factory,
        excluded_provider_ids={'codex-subscription', 'cerebras'},
    )
    assert [definition.id for definition in without_codex.definitions] == ['openai']

    custom_binding = StoredAPIKeyBinding(MemoryStore[SecretStr]())
    custom = ProviderDefinition(
        id='openai',
        label='Custom OpenAI',
        flows=(APIKeyAuthenticationFlow(binding=custom_binding),),
        credentials=custom_binding,
        load_models=provider_loader(provider_option('openai')),
    )
    overridden = default_provider_registry(
        api_key_store=api_key_store,
        codex_auth=codex_auth,
        codex_store=codex_store,
        http_client=http_client,
        model_factory=model_factory,
        custom_definitions=(custom,),
    )
    assert overridden.provider('openai') is custom
    with pytest.raises(ValueError, match='custom provider definition ids'):
        default_provider_registry(
            api_key_store=api_key_store,
            codex_auth=codex_auth,
            codex_store=codex_store,
            http_client=http_client,
            model_factory=model_factory,
            custom_definitions=(custom, custom),
        )


class MemoryStore[Credential]:
    def __init__(self) -> None:
        self.value: Credential | None = None

    async def load(self) -> Credential | None:
        return self.value

    async def save(self, credential: Credential, /) -> None:
        self.value = credential

    async def delete(self) -> None:
        self.value = None


def provider_option(provider: str) -> ModelProviderOption:
    return ModelProviderOption(
        value=provider,
        label=provider,
        models=(SelectionOption(value='model', label='model'),),
    )


def provider_loader(option: ModelProviderOption) -> Callable[[], Awaitable[ModelProviderOption]]:
    async def load() -> ModelProviderOption:
        return option

    return load
