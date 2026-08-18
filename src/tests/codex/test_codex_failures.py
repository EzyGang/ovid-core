import asyncio

import httpx
import pytest
from pydantic import SecretStr

from ovid_core import CodexAuthError, ModelResolutionError
from ovid_core.adapters.pydantic_ai import CodexSubscriptionModelFactory
from ovid_core.adapters.pydantic_ai.codex import _prepare_request, _RedactingTransport
from ovid_core.codex import CodexAuth, CodexOAuthConfig, CodexTokens, codex_account_id
from ovid_core.config import ModelConfig
from tests.support.helpers import MemoryTokenStore, make_codex_tokens, make_jwt, oauth_client


def device_start_response() -> httpx.Response:
    return httpx.Response(200, json={'device_auth_id': 'secret-device', 'user_code': 'CODE', 'interval': 0.001})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('response', 'message'),
    [
        (httpx.Response(500), 'status 500'),
        (httpx.Response(200, json={'device_auth_id': 'id', 'user_code': 'code', 'interval': 0}), 'failed'),
        (httpx.Response(200, json={'device_auth_id': 'id', 'user_code': 'code', 'interval': 'nan'}), 'failed'),
        (httpx.Response(200, json={'device_auth_id': 'id', 'user_code': 'code', 'interval': 'inf'}), 'failed'),
        (httpx.Response(200, content=b'invalid'), 'failed'),
    ],
)
async def test_device_start_failures_are_safe(response: httpx.Response, message: str) -> None:
    async with oauth_client(lambda request: response) as client, CodexAuth.ephemeral(http_client=client) as auth:
        with pytest.raises(CodexAuthError, match=message) as captured:
            await auth.start_device_login()
    assert 'invalid' not in repr(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('token_response', [httpx.Response(500), httpx.Response(200, content=b'invalid')])
async def test_device_token_exchange_failures_are_safe(token_response: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith('/deviceauth/usercode'):
            return device_start_response()
        if request.url.path.endswith('/deviceauth/token'):
            return httpx.Response(
                200,
                json={'authorization_code': 'secret-code', 'code_challenge': 'unused', 'code_verifier': 'verifier'},
            )

        return token_response

    async with oauth_client(handler) as client, CodexAuth.ephemeral(http_client=client) as auth:
        login = await auth.start_device_login()
        with pytest.raises(CodexAuthError) as captured:
            await login.wait()
    assert 'secret-code' not in repr(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('poll_response', [httpx.Response(500), httpx.Response(200, content=b'invalid')])
async def test_device_poll_failures_are_safe(poll_response: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return device_start_response() if request.url.path.endswith('/deviceauth/usercode') else poll_response

    async with oauth_client(handler) as client, CodexAuth.ephemeral(http_client=client) as auth:
        login = await auth.start_device_login()
        with pytest.raises(CodexAuthError) as captured:
            await login.wait()
    assert 'secret-device' not in repr(captured.value)


@pytest.mark.asyncio
async def test_device_poll_timeout_is_explicit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return device_start_response() if request.url.path.endswith('/deviceauth/usercode') else httpx.Response(403)

    config = CodexOAuthConfig(login_timeout_seconds=0.001)
    async with oauth_client(handler) as client, CodexAuth.ephemeral(http_client=client, config=config) as auth:
        login = await auth.start_device_login()
        with pytest.raises(CodexAuthError, match='timed out'):
            await login.wait()


@pytest.mark.asyncio
async def test_device_login_timeout_bounds_stalled_requests() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith('/deviceauth/usercode'):
            return device_start_response()

        await asyncio.Event().wait()
        raise AssertionError('The timed out request resumed')

    config = CodexOAuthConfig(login_timeout_seconds=0.001)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None) as client,
        CodexAuth.ephemeral(http_client=client, config=config) as auth,
    ):
        login = await auth.start_device_login()
        with pytest.raises(CodexAuthError, match='timed out'):
            await login.wait()


@pytest.mark.asyncio
async def test_device_start_timeout_is_explicit() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError('The timed out request resumed')

    config = CodexOAuthConfig(login_timeout_seconds=0.001)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None) as client,
        CodexAuth.ephemeral(http_client=client, config=config) as auth,
    ):
        with pytest.raises(CodexAuthError, match='timed out'):
            await auth.start_device_login()


@pytest.mark.asyncio
async def test_auth_rejects_missing_malformed_and_failed_refreshes() -> None:
    config = CodexOAuthConfig()
    async with oauth_client(lambda request: httpx.Response(500, text='refresh-secret')) as client:
        async with CodexAuth(store=MemoryTokenStore(), http_client=client, config=config) as missing:
            with pytest.raises(CodexAuthError, match='required'):
                await missing._request_tokens()

        store = MemoryTokenStore(make_codex_tokens(expired=True))
        async with CodexAuth(store=store, http_client=client, config=config) as failed:
            with pytest.raises(CodexAuthError, match='status 500') as captured:
                await failed._request_tokens()
            assert 'refresh-secret' not in repr(captured.value)

    async with oauth_client(lambda request: httpx.Response(200, content=b'invalid')) as client:
        store = MemoryTokenStore(make_codex_tokens(expired=True))
        async with CodexAuth(store=store, http_client=client, config=config) as invalid:
            with pytest.raises(CodexAuthError, match='refresh failed'):
                await invalid._request_tokens()


@pytest.mark.asyncio
async def test_partial_refresh_preserves_omitted_rotating_tokens() -> None:
    original = make_codex_tokens(expired=True)
    new_access = make_codex_tokens(suffix='new').access_token
    store = MemoryTokenStore(original)

    async with oauth_client(
        lambda request: httpx.Response(200, json={'access_token': new_access.get_secret_value()})
    ) as client:
        async with CodexAuth(store=store, http_client=client) as auth:
            updated = await auth._request_tokens()

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
@pytest.mark.parametrize(
    'access_token', ['bad', 'header.a.signature', make_jwt({'sub': 'user'}), make_jwt({'exp': True})]
)
async def test_access_token_validation_is_safe(access_token: str) -> None:
    tokens = CodexTokens(
        id_token=make_codex_tokens().id_token,
        access_token=SecretStr(access_token),
        refresh_token=SecretStr('secret'),
    )
    store = MemoryTokenStore(tokens)
    async with (
        oauth_client(lambda request: httpx.Response(500)) as client,
        CodexAuth(store=store, http_client=client) as auth,
    ):
        with pytest.raises(CodexAuthError):
            await auth._request_tokens()


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
    store = MemoryTokenStore(make_codex_tokens())
    backend = httpx.MockTransport(lambda request: httpx.Response(status_code, content=content))
    async with (
        oauth_client(lambda request: httpx.Response(500)) as client,
        CodexAuth(store=store, http_client=client) as auth,
    ):
        factory = CodexSubscriptionModelFactory(auth=auth, backend_transport=backend)
        with pytest.raises(ModelResolutionError) as captured:
            await factory.build(
                model_id='codex',
                config=ModelConfig(provider='codex-subscription', model='gpt-5-codex'),
            )

    assert 'catalog-secret' not in repr(captured.value)
