import httpx
import pytest
from keyring.errors import KeyringError
from pydantic import SecretStr
from pytest_mock import MockerFixture

from ovid_core import CodexAuthError, ModelResolutionError
from ovid_core.adapters.pydantic_ai import CodexSubscriptionModelFactory
from ovid_core.adapters.pydantic_ai.codex import _prepare_request, _RedactingTransport
from ovid_core.codex import (
    CodexDeviceAuthClient,
    CodexDeviceAuthorization,
    CodexOAuthConfig,
    CodexTokenManager,
    CodexTokens,
    KeyringCodexTokenStore,
    codex_account_id,
)
from ovid_core.config import ModelConfig
from tests.support.helpers import MemoryTokenStore, make_codex_tokens, make_jwt, oauth_client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('response', 'message'),
    [
        (httpx.Response(500), 'status 500'),
        (httpx.Response(200, json={'device_auth_id': 'id', 'user_code': 'code', 'interval': 0}), 'failed'),
        (httpx.Response(200, content=b'invalid'), 'failed'),
    ],
)
async def test_device_start_failures_are_safe(response: httpx.Response, message: str) -> None:
    async with oauth_client(lambda request: response) as client:
        config = CodexOAuthConfig()
        manager = CodexTokenManager(store=MemoryTokenStore(), http_client=client, config=config)
        device = CodexDeviceAuthClient(http_client=client, token_manager=manager, config=config)
        with pytest.raises(CodexAuthError, match=message) as captured:
            await device.start()
    assert 'invalid' not in repr(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('token_response', [httpx.Response(500), httpx.Response(200, content=b'invalid')])
async def test_device_token_exchange_failures_are_safe(token_response: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith('/deviceauth/token'):
            return httpx.Response(
                200,
                json={'authorization_code': 'secret-code', 'code_challenge': 'challenge', 'code_verifier': 'verifier'},
            )
        return token_response

    async with oauth_client(handler) as client:
        config = CodexOAuthConfig()
        manager = CodexTokenManager(store=MemoryTokenStore(), http_client=client, config=config)
        device = CodexDeviceAuthClient(http_client=client, token_manager=manager, config=config)
        authorization = CodexDeviceAuthorization(
            verification_url='https://auth.openai.com/codex/device',
            user_code='code',
            device_auth_id='secret-device',
            interval_seconds=0.001,
        )
        with pytest.raises(CodexAuthError) as captured:
            await device.complete(authorization)
    assert 'secret-code' not in repr(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('poll_response', [httpx.Response(500), httpx.Response(200, content=b'invalid')])
async def test_device_poll_failures_are_safe(poll_response: httpx.Response) -> None:
    async with oauth_client(lambda request: poll_response) as client:
        config = CodexOAuthConfig()
        manager = CodexTokenManager(store=MemoryTokenStore(), http_client=client, config=config)
        device = CodexDeviceAuthClient(http_client=client, token_manager=manager, config=config)
        authorization = CodexDeviceAuthorization(
            verification_url='https://auth.openai.com/codex/device',
            user_code='code',
            device_auth_id='secret-device',
            interval_seconds=0.001,
        )
        with pytest.raises(CodexAuthError) as captured:
            await device.complete(authorization)
    assert 'secret-device' not in repr(captured.value)


@pytest.mark.asyncio
async def test_device_poll_timeout_is_explicit() -> None:
    async with oauth_client(lambda request: httpx.Response(403)) as client:
        config = CodexOAuthConfig(poll_timeout_seconds=0.001)
        manager = CodexTokenManager(store=MemoryTokenStore(), http_client=client, config=config)
        device = CodexDeviceAuthClient(http_client=client, token_manager=manager, config=config)
        authorization = CodexDeviceAuthorization(
            verification_url='https://auth.openai.com/codex/device',
            user_code='code',
            device_auth_id='device',
            interval_seconds=0.001,
        )
        with pytest.raises(CodexAuthError, match='timed out'):
            await device.complete(authorization)


@pytest.mark.asyncio
async def test_token_manager_rejects_missing_malformed_and_failed_refreshes() -> None:
    async with oauth_client(lambda request: httpx.Response(500, text='refresh-secret')) as client:
        config = CodexOAuthConfig()
        missing = CodexTokenManager(store=MemoryTokenStore(), http_client=client, config=config)
        with pytest.raises(CodexAuthError, match='required'):
            await missing.tokens()

        failed = CodexTokenManager(
            store=MemoryTokenStore(make_codex_tokens(expired=True)), http_client=client, config=config
        )
        with pytest.raises(CodexAuthError, match='status 500') as captured:
            await failed.tokens()
        assert 'refresh-secret' not in repr(captured.value)

    async with oauth_client(lambda request: httpx.Response(200, content=b'invalid')) as client:
        invalid = CodexTokenManager(
            store=MemoryTokenStore(make_codex_tokens(expired=True)), http_client=client, config=config
        )
        with pytest.raises(CodexAuthError, match='refresh failed'):
            await invalid.tokens()


@pytest.mark.asyncio
async def test_partial_refresh_preserves_omitted_rotating_tokens() -> None:
    original = make_codex_tokens(expired=True)
    new_access = make_codex_tokens(suffix='new').access_token

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={'access_token': new_access.get_secret_value()})

    store = MemoryTokenStore(original)
    async with oauth_client(handler) as client:
        manager = CodexTokenManager(store=store, http_client=client, config=CodexOAuthConfig())
        updated = await manager.tokens()
    assert updated.id_token == original.id_token
    assert updated.access_token == new_access
    assert updated.refresh_token == original.refresh_token


@pytest.mark.parametrize(
    'tokens',
    [
        CodexTokens(id_token=SecretStr('bad'), access_token=SecretStr('bad'), refresh_token=SecretStr('secret')),
        CodexTokens(
            id_token=SecretStr(make_jwt({'sub': 'user'})),
            access_token=SecretStr(make_jwt({'exp': 9999999999})),
            refresh_token=SecretStr('secret'),
        ),
        CodexTokens(
            id_token=SecretStr(make_jwt({'https://api.openai.com/auth': {}})),
            access_token=SecretStr(make_jwt({'exp': 9999999999})),
            refresh_token=SecretStr('secret'),
        ),
    ],
)
def test_account_claim_validation_is_safe(tokens: CodexTokens) -> None:
    with pytest.raises(CodexAuthError):
        codex_account_id(tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize('access_token', ['bad', make_jwt({'sub': 'user'}), make_jwt({'exp': True})])
async def test_access_token_validation_is_safe(access_token: str) -> None:
    tokens = CodexTokens(
        id_token=make_codex_tokens().id_token,
        access_token=SecretStr(access_token),
        refresh_token=SecretStr('secret'),
    )
    async with oauth_client(lambda request: httpx.Response(500)) as client:
        manager = CodexTokenManager(store=MemoryTokenStore(tokens), http_client=client, config=CodexOAuthConfig())
        with pytest.raises(CodexAuthError):
            await manager.tokens()


def test_codex_request_authentication_preserves_responses_payloads() -> None:
    tokens = make_codex_tokens()
    content = b'{"input":[{"type":"item_reference","id":"response-item"}],"store":false,"stream":true}'
    request = httpx.Request(
        'POST',
        'https://chatgpt.com/backend-api/codex/responses',
        headers={'x-api-key': 'remove'},
        content=content,
    )

    prepared = _prepare_request(request, tokens)

    assert prepared is request
    assert prepared.content == content
    assert 'x-api-key' not in prepared.headers
    assert prepared.headers['authorization'].endswith(tokens.access_token.get_secret_value())
    assert prepared.headers['chatgpt-account-id'] == codex_account_id(tokens)


@pytest.mark.asyncio
async def test_backend_transport_redacts_network_failures() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('signed-url-secret', request=request)

    transport = _RedactingTransport(httpx.MockTransport(fail))
    with pytest.raises(httpx.TransportError) as captured:
        await transport.handle_async_request(httpx.Request('POST', 'https://chatgpt.example/responses'))
    await transport.aclose()
    assert 'signed-url-secret' not in repr(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('status_code', 'content'),
    [
        (200, b'{"models":[{"slug":"other","base_instructions":"valid"}]}'),
        (200, b'{"models":[{"slug":"gpt-5-codex","base_instructions":""}]}'),
        (500, b'catalog-secret'),
    ],
)
async def test_subscription_factory_rejects_invalid_model_catalogs(status_code: int, content: bytes) -> None:
    def backend_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=content)

    async with oauth_client(lambda request: httpx.Response(500)) as client:
        manager = CodexTokenManager(
            store=MemoryTokenStore(make_codex_tokens()),
            http_client=client,
            config=CodexOAuthConfig(),
        )
        factory = CodexSubscriptionModelFactory(
            token_manager=manager,
            backend_transport=httpx.MockTransport(backend_handler),
        )
        with pytest.raises(ModelResolutionError) as captured:
            await factory.build(
                model_id='codex',
                config=ModelConfig(provider='codex-subscription', model='gpt-5-codex'),
            )

    assert 'catalog-secret' not in repr(captured.value)


@pytest.mark.asyncio
async def test_keyring_write_delete_and_payload_failures_are_safe(mocker: MockerFixture) -> None:
    store = KeyringCodexTokenStore(service='test', account='account')
    get_password = mocker.patch('keyring.get_password', return_value='{"id_token":"only"}')
    with pytest.raises(CodexAuthError):
        await store.load()

    def fail(*args: str) -> None:
        raise KeyringError('backend-secret')

    mocker.patch('keyring.set_password', side_effect=fail)
    with pytest.raises(CodexAuthError) as save_error:
        await store.save(make_codex_tokens())
    get_password.return_value = 'stored'
    mocker.patch('keyring.delete_password', side_effect=fail)
    with pytest.raises(CodexAuthError) as delete_error:
        await store.delete()
    assert 'backend-secret' not in repr(save_error.value)
    assert 'backend-secret' not in repr(delete_error.value)
