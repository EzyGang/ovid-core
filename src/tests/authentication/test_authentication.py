from collections.abc import Awaitable, Callable

import pytest
from pydantic import SecretStr, TypeAdapter

from ovid_core.authentication import (
    APIKeyAuthenticationFlow,
    AuthenticationFlowDescriptor,
    AuthenticationSessionId,
    AuthenticationSessionStarted,
    BrowserAuthorizationAuthenticationInteraction,
    CompletedAuthenticationInteraction,
    DeviceAuthorizationAuthenticationInteraction,
    FailedAuthenticationInteraction,
    InputRequestedAuthenticationInteraction,
    ProgressAuthenticationInteraction,
    ProviderDefinition,
    ProviderRegistry,
    StoredAPIKeyBinding,
    api_key_provider_definition,
)
from ovid_core.errors import AuthenticationError
from ovid_core.routing import ModelProviderOption, SelectionOption


async def test_api_key_flow_persists_through_generic_binding() -> None:
    store = MemoryStore[SecretStr]()
    binding = StoredAPIKeyBinding(store)
    flow = APIKeyAuthenticationFlow(binding=binding)
    with pytest.raises(ValueError, match='must not be empty'):
        await binding.save_api_key(SecretStr(''))
    session = await flow.create_session()

    interaction = await session.start()
    assert interaction == InputRequestedAuthenticationInteraction(input_kind='api_key')
    assert await session.poll() is None
    with pytest.raises(AuthenticationError, match='already started'):
        await session.start()
    with pytest.raises(AuthenticationError, match='must not be empty'):
        await session.submit(SecretStr(''))

    completed = await session.submit(SecretStr('secret'))
    assert completed == CompletedAuthenticationInteraction()
    assert await binding.connected()
    saved = await store.load()
    assert saved is not None and saved.get_secret_value() == 'secret'
    with pytest.raises(AuthenticationError, match='not waiting'):
        await session.submit(SecretStr('again'))

    await binding.disconnect()
    assert not await binding.connected()
    cancelled = await flow.create_session()
    await cancelled.cancel()
    with pytest.raises(AuthenticationError, match='not waiting'):
        await cancelled.submit(SecretStr('secret'))


async def test_provider_registry_derives_status_sessions_and_models() -> None:
    first_store = MemoryStore[SecretStr]()
    first = api_key_provider_definition(options=provider_option('openai'), binding=StoredAPIKeyBinding(first_store))
    second = api_key_provider_definition(
        options=provider_option('cerebras'), binding=StoredAPIKeyBinding(MemoryStore())
    )
    registry = ProviderRegistry((first, second))
    assert registry.definitions == (first, second)

    status = await registry.status({'cerebras': 'failed'})
    assert not status.configured
    assert [provider.id for provider in status.providers] == ['openai', 'cerebras']
    assert status.providers[1].state == 'failed'
    assert status.providers[0].flows == (AuthenticationFlowDescriptor(id='api-key', label='API key'),)
    with pytest.raises(AuthenticationError, match='No model provider'):
        await registry.model_options()
    with pytest.raises(AuthenticationError, match='not available'):
        registry.provider('missing')
    with pytest.raises(AuthenticationError, match='not available'):
        await first.create_session('missing')

    session = await first.create_session('api-key')
    await session.start()
    await session.submit(SecretStr('secret'))
    options = await registry.model_options()
    assert [provider.value for provider in options.providers] == ['openai']
    assert options.reasoning_efforts[-1].value == 'xhigh'

    await registry.provider('openai').disconnect()
    assert not (await registry.status()).configured


async def test_provider_registry_rejects_invalid_definitions() -> None:
    binding = StoredAPIKeyBinding(MemoryStore[SecretStr]())
    flow = APIKeyAuthenticationFlow(binding=binding)
    loader = provider_loader(provider_option('openai'))

    with pytest.raises(ValueError, match='must not be empty'):
        ProviderDefinition(id='', label='OpenAI', flows=(flow,), credentials=binding, load_models=loader)
    with pytest.raises(ValueError, match='unique authentication flows'):
        ProviderDefinition(id='openai', label='OpenAI', flows=(), credentials=binding, load_models=loader)
    with pytest.raises(ValueError, match='unique authentication flows'):
        ProviderDefinition(id='openai', label='OpenAI', flows=(flow, flow), credentials=binding, load_models=loader)
    definition = ProviderDefinition(
        id='openai',
        label='OpenAI',
        flows=(flow,),
        credentials=binding,
        load_models=loader,
    )
    with pytest.raises(ValueError, match='at least one'):
        ProviderRegistry(())
    with pytest.raises(ValueError, match='ids must be unique'):
        ProviderRegistry((definition, definition))

    wrong = ProviderDefinition(
        id='other',
        label='Other',
        flows=(flow,),
        credentials=binding,
        load_models=loader,
    )
    with pytest.raises(AuthenticationError, match='different provider'):
        await wrong.models()


async def test_authentication_interactions_and_identifiers_serialize() -> None:
    session_id = AuthenticationSessionId.new()
    interactions = (
        BrowserAuthorizationAuthenticationInteraction(
            url='https://auth.example',
            manual_callback=True,
        ),
        DeviceAuthorizationAuthenticationInteraction(
            url='https://auth.example/device',
            user_code='CODE',
        ),
        ProgressAuthenticationInteraction(stage='waiting'),
        CompletedAuthenticationInteraction(),
        FailedAuthenticationInteraction(reason='provider_error'),
    )
    adapter = TypeAdapter(AuthenticationSessionStarted)

    for interaction in interactions:
        value = AuthenticationSessionStarted(session_id=session_id, interaction=interaction)
        assert adapter.validate_json(value.model_dump_json()) == value
    assert str(session_id) == str(session_id.root)


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
