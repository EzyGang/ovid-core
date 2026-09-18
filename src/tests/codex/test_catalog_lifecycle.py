import asyncio

import httpx
import pytest

from ovid_core.adapters.pydantic_ai import CodexSubscriptionModelFactory
from ovid_core.codex import CodexAuth, MemoryCodexTokenStore
from ovid_core.config import ModelConfig
from ovid_core.errors import CodexAuthError
from tests.support.helpers import make_codex_tokens


@pytest.mark.asyncio
async def test_provider_catalog_observes_replacement_and_disconnect() -> None:
    store = MemoryCodexTokenStore()
    first = make_codex_tokens()
    second = make_codex_tokens(suffix='replacement')
    await store.save(first)

    def backend(request: httpx.Request) -> httpx.Response:
        model = (
            'first' if request.headers['authorization'].endswith(first.access_token.get_secret_value()) else 'second'
        )
        return httpx.Response(200, json={'models': [{'slug': model, 'base_instructions': model}]})

    async with CodexAuth(store=store) as auth:
        factory = CodexSubscriptionModelFactory(auth=auth, backend_transport=httpx.MockTransport(backend))
        assert tuple(model.value for model in (await factory.provider_options()).models) == ('first',)
        await store.save(second)
        assert tuple(model.value for model in (await factory.provider_options()).models) == ('second',)
        await store.delete()
        with pytest.raises(CodexAuthError):
            await factory.provider_options()


async def test_model_activation_auth_failure_recovers_after_credential_replacement() -> None:
    store = MemoryCodexTokenStore()
    await store.save(make_codex_tokens())
    rejected = True

    def backend(_request: httpx.Request) -> httpx.Response:
        if rejected:
            return httpx.Response(403, json={'error': 'provider-secret'})
        return httpx.Response(200, json={'models': [{'slug': 'gpt-5', 'base_instructions': 'Instructions'}]})

    async with CodexAuth(store=store) as auth:
        factory = CodexSubscriptionModelFactory(auth=auth, backend_transport=httpx.MockTransport(backend))
        with pytest.raises(CodexAuthError) as failure:
            await factory.build(model_id='primary', config=ModelConfig(provider='codex-subscription', model='gpt-5'))
        assert 'provider-secret' not in repr(failure.value)
        rejected = False
        await store.save(make_codex_tokens(suffix='replacement'))
        assert tuple(model.value for model in (await factory.provider_options()).models) == ('gpt-5',)


async def test_cancelled_activation_can_be_retried_without_a_cached_failure() -> None:
    store = MemoryCodexTokenStore()
    await store.save(make_codex_tokens())
    started = asyncio.Event()
    release = asyncio.Event()

    async def backend(_request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(200, json={'models': [{'slug': 'gpt-5', 'base_instructions': 'Instructions'}]})

    async with CodexAuth(store=store) as auth:
        factory = CodexSubscriptionModelFactory(auth=auth, backend_transport=httpx.MockTransport(backend))
        activation = asyncio.create_task(
            factory.build(model_id='primary', config=ModelConfig(provider='codex-subscription', model='gpt-5'))
        )
        await asyncio.wait_for(started.wait(), 2)
        activation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await activation
        release.set()
        assert tuple(model.value for model in (await factory.provider_options()).models) == ('gpt-5',)
