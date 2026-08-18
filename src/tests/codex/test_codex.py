import json
import time

import httpx
import pytest
from keyring.errors import KeyringError
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIResponsesModelSettings
from pytest_mock import MockerFixture

from ovid_core import CodexAuthError, ModelResolutionError
from ovid_core.adapters.pydantic_ai import CodexSubscriptionModelFactory
from ovid_core.codex import CodexAuth, KeyringCodexTokenStore
from ovid_core.config import ModelConfig
from tests.support.helpers import MemoryTokenStore, json_body, make_codex_tokens, oauth_client


@pytest.mark.asyncio
async def test_subscription_factory_runs_stateless_responses_and_retries_unauthorized() -> None:
    backend_requests: list[httpx.Request] = []
    response_requests: list[httpx.Request] = []
    refreshed = make_codex_tokens(suffix='new')

    def oauth_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                'id_token': refreshed.id_token.get_secret_value(),
                'access_token': refreshed.access_token.get_secret_value(),
                'refresh_token': refreshed.refresh_token.get_secret_value(),
            },
        )

    def backend_handler(request: httpx.Request) -> httpx.Response:
        backend_requests.append(request)
        if request.url.path.endswith('/models'):
            return httpx.Response(
                200,
                json={'models': [{'slug': 'gpt-5-codex', 'base_instructions': 'approved-codex-instructions'}]},
            )
        response_requests.append(request)
        if len(response_requests) == 1:
            return httpx.Response(401, json={'error': 'expired bearer-secret'})
        response = {
            'id': 'resp_1',
            'created_at': time.time(),
            'model': 'gpt-5-codex',
            'object': 'response',
            'output': [],
            'parallel_tool_calls': True,
            'tool_choice': 'auto',
            'tools': [],
            'status': 'completed',
        }
        events = (
            {'type': 'response.created', 'sequence_number': 0, 'response': response},
            {
                'type': 'response.output_text.delta',
                'sequence_number': 1,
                'item_id': 'message_1',
                'output_index': 0,
                'content_index': 0,
                'delta': 'subscription works',
                'logprobs': [],
            },
            {'type': 'response.completed', 'sequence_number': 2, 'response': response},
        )
        content = ''.join(f'data: {json.dumps(event)}\n\n' for event in events)

        return httpx.Response(200, text=content, headers={'content-type': 'text/event-stream'})

    store = MemoryTokenStore(make_codex_tokens())
    async with oauth_client(oauth_handler) as oauth_http_client:
        async with CodexAuth(store=store, http_client=oauth_http_client) as auth:
            factory = CodexSubscriptionModelFactory(
                auth=auth,
                backend_transport=httpx.MockTransport(backend_handler),
            )
            model_config = ModelConfig(provider='codex-subscription', model='gpt-5-codex')
            handle = await factory.build(model_id='codex', config=model_config)
            cached_handle = await factory.build(model_id='codex-cached', config=model_config)
            async with handle._runtime, cached_handle._runtime:
                agent = Agent(handle._runtime, instructions='custom-agent-guidance')
                result = await agent.run('hello')
                repeated = await agent.run('again', message_history=result.all_messages())
                plain = await Agent(handle._runtime).run('without custom instructions')
                streaming_agent = Agent(
                    handle._runtime,
                    model_settings=OpenAIResponsesModelSettings(openai_store=True),
                )
                async with streaming_agent.run_stream('stream directly') as streamed:
                    streamed_output = await streamed.get_output()

    assert result.output == 'subscription works'
    assert repeated.output == 'subscription works'
    assert plain.output == 'subscription works'
    assert streamed_output == 'subscription works'
    assert len(backend_requests) == 6
    assert backend_requests[0].url.path.endswith('/models')
    assert len(response_requests) == 5
    first_body = json_body(response_requests[0])
    assert first_body['instructions'] == 'approved-codex-instructions'
    assert first_body['store'] is False
    assert first_body['include'] == ['reasoning.encrypted_content']
    assert 'custom-agent-guidance' in repr(first_body['input'])
    assert 'developer' in repr(first_body['input'])
    assert json_body(response_requests[2])['input'] != json_body(response_requests[1])['input']
    assert json_body(response_requests[3])['instructions'] == 'approved-codex-instructions'
    assert json_body(response_requests[4])['store'] is False
    assert backend_requests[0].url.params['client_version'] == '0.0.0'
    assert response_requests[0].headers['chatgpt-account-id'] == 'account-1'
    assert response_requests[1].headers['authorization'].endswith(refreshed.access_token.get_secret_value())


@pytest.mark.asyncio
async def test_factory_delegates_non_subscription_models_and_rejects_stateful_settings() -> None:
    store = MemoryTokenStore(make_codex_tokens())
    async with oauth_client(lambda request: httpx.Response(500)) as client:
        async with CodexAuth(store=store, http_client=client) as auth:
            factory = CodexSubscriptionModelFactory(auth=auth)
            delegated = await factory.build(model_id='test', config=ModelConfig(provider='test', model='test'))
            with pytest.raises(ModelResolutionError) as captured:
                await factory.build(
                    model_id='unsafe',
                    config=ModelConfig(
                        provider='codex-subscription',
                        model='gpt-5-codex',
                        settings={'openai_previous_response_id': 'secret-response'},
                    ),
                )

    assert isinstance(delegated._runtime, Model)
    assert 'secret-response' not in repr(captured.value)


@pytest.mark.asyncio
async def test_keyring_store_round_trip_and_safe_errors(mocker: MockerFixture) -> None:
    values: dict[tuple[str, str], str] = {}
    get_password = mocker.patch(
        'keyring.get_password',
        side_effect=lambda service, account: values.get((service, account)),
    )
    mocker.patch(
        'keyring.set_password',
        side_effect=lambda service, account, value: values.__setitem__((service, account), value),
    )
    mocker.patch('keyring.delete_password', side_effect=lambda service, account: values.pop((service, account)))
    store = KeyringCodexTokenStore(service='test', account='user')

    assert await store.load() is None
    await store.save(make_codex_tokens())
    assert await store.load() == make_codex_tokens()
    await store.delete()
    assert await store.load() is None
    await store.delete()

    def fail_get(service: str, account: str) -> str:
        raise KeyringError('secret backend detail')

    get_password.side_effect = fail_get
    with pytest.raises(CodexAuthError) as captured:
        await store.load()
    assert 'secret backend detail' not in repr(captured.value)
