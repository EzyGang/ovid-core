import json
import time

import httpx
import pytest
from keyring.errors import KeyringError
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIResponsesModelSettings

from ovid_core.adapters.pydantic_ai.codex import CodexSubscriptionModelFactory
from ovid_core.codex.device import CodexDeviceAuthClient
from ovid_core.codex.keyring import KeyringCodexTokenStore
from ovid_core.codex.models import CodexOAuthConfig
from ovid_core.codex.tokens import CodexTokenManager
from ovid_core.config.models import ModelConfig
from ovid_core.errors import CodexAuthError, ModelResolutionError
from tests.helpers import MemoryTokenStore, json_body, make_codex_tokens, oauth_client


@pytest.mark.asyncio
async def test_device_flow_polls_exchanges_and_persists_redacted_tokens() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith('/deviceauth/usercode'):
            return httpx.Response(
                200, json={'device_auth_id': 'device-secret', 'user_code': 'ABCD', 'interval': '0.001'}
            )
        if request.url.path.endswith('/deviceauth/token') and len(requests) == 2:
            return httpx.Response(403)
        if request.url.path.endswith('/deviceauth/token'):
            return httpx.Response(
                200,
                json={'authorization_code': 'code-secret', 'code_challenge': 'challenge', 'code_verifier': 'verifier'},
            )
        assert request.url.path.endswith('/oauth/token')
        tokens = make_codex_tokens()
        return httpx.Response(
            200,
            json={
                'id_token': tokens.id_token.get_secret_value(),
                'access_token': tokens.access_token.get_secret_value(),
                'refresh_token': tokens.refresh_token.get_secret_value(),
                'token_type': 'Bearer',
            },
        )

    async with oauth_client(handler) as client:
        store = MemoryTokenStore()
        config = CodexOAuthConfig(issuer='https://auth.example', poll_timeout_seconds=1)
        manager = CodexTokenManager(store=store, http_client=client, config=config)
        device = CodexDeviceAuthClient(http_client=client, token_manager=manager, config=config)
        authorization = await device.start()
        tokens = await device.complete(authorization)

    assert authorization.verification_url == 'https://auth.example/codex/device'
    assert authorization.user_code == 'ABCD'
    assert 'device-secret' not in repr(authorization)
    assert authorization.model_dump() == {
        'verification_url': 'https://auth.example/codex/device',
        'user_code': 'ABCD',
    }
    assert store.value == tokens
    assert 'refresh-old' not in repr(tokens)
    assert 'refresh-old' not in tokens.model_dump_json()
    assert json_body(requests[0]) == {'client_id': config.client_id}
    assert json_body(requests[1]) == {'device_auth_id': 'device-secret', 'user_code': 'ABCD'}
    assert b'code-secret' in requests[-1].content
    assert [request.headers['content-type'] for request in requests] == [
        'application/json',
        'application/json',
        'application/json',
        'application/x-www-form-urlencoded',
    ]


@pytest.mark.asyncio
async def test_token_manager_refreshes_expiring_tokens_and_logs_out() -> None:
    refreshed = make_codex_tokens(suffix='new')
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                'id_token': refreshed.id_token.get_secret_value(),
                'access_token': refreshed.access_token.get_secret_value(),
                'refresh_token': refreshed.refresh_token.get_secret_value(),
            },
        )

    store = MemoryTokenStore(make_codex_tokens(expired=True))
    async with oauth_client(handler) as client:
        manager = CodexTokenManager(store=store, http_client=client, config=CodexOAuthConfig())
        assert await manager.tokens() == refreshed
        assert await manager.tokens() == refreshed
        await manager.logout()

    assert len(requests) == 1
    assert json_body(requests[0])['grant_type'] == 'refresh_token'
    assert requests[0].headers['content-type'] == 'application/json'
    assert store.value is None


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

    async with oauth_client(oauth_handler) as oauth_http_client:
        manager = CodexTokenManager(
            store=MemoryTokenStore(make_codex_tokens()), http_client=oauth_http_client, config=CodexOAuthConfig()
        )
        factory = CodexSubscriptionModelFactory(
            token_manager=manager,
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
    async with oauth_client(lambda request: httpx.Response(500)) as client:
        manager = CodexTokenManager(
            store=MemoryTokenStore(make_codex_tokens()), http_client=client, config=CodexOAuthConfig()
        )
        factory = CodexSubscriptionModelFactory(token_manager=manager)
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
async def test_keyring_store_round_trip_and_safe_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    values: dict[tuple[str, str], str] = {}
    monkeypatch.setattr('keyring.get_password', lambda service, account: values.get((service, account)))
    monkeypatch.setattr(
        'keyring.set_password', lambda service, account, value: values.__setitem__((service, account), value)
    )
    monkeypatch.setattr('keyring.delete_password', lambda service, account: values.pop((service, account)))
    store = KeyringCodexTokenStore(service='test', account='user')

    assert await store.load() is None
    await store.save(make_codex_tokens())
    assert await store.load() == make_codex_tokens()
    await store.delete()
    assert await store.load() is None
    await store.delete()

    def fail_get(service: str, account: str) -> str:
        raise KeyringError('secret backend detail')

    monkeypatch.setattr('keyring.get_password', fail_get)
    with pytest.raises(CodexAuthError) as captured:
        await store.load()
    assert 'secret backend detail' not in repr(captured.value)
