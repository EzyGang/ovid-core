import asyncio
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ovid_core import CodexAuthError
from ovid_core.codex import CodexAuth, CodexOAuthConfig, MemoryCodexTokenStore
from tests.support.helpers import MemoryTokenStore, codex_token_response, json_body, make_codex_tokens, oauth_client


@pytest.mark.asyncio
async def test_memory_token_store_round_trip() -> None:
    store = MemoryCodexTokenStore()
    tokens = make_codex_tokens()

    assert await store.load() is None
    await store.save(tokens)
    assert await store.load() == tokens
    await store.delete()
    assert await store.load() is None


@pytest.mark.asyncio
async def test_device_login_polls_exchanges_and_persists_tokens() -> None:
    requests: list[httpx.Request] = []
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        requests.append(request)
        if request.url.path.endswith('/deviceauth/usercode'):
            return httpx.Response(
                200, json={'device_auth_id': 'device-secret', 'user_code': 'ABCD', 'interval': '0.001'}
            )
        if request.url.path.endswith('/deviceauth/token'):
            polls += 1
            if polls == 1:
                return httpx.Response(403)
            return httpx.Response(
                200,
                json={'authorization_code': 'code-secret', 'code_challenge': 'unused', 'code_verifier': 'verifier'},
            )

        return codex_token_response()

    store = MemoryTokenStore()
    config = CodexOAuthConfig(issuer='https://auth.example', login_timeout_seconds=1)
    async with oauth_client(handler) as client, CodexAuth(store=store, http_client=client, config=config) as auth:
        login = await auth.start_device_login()
        assert login.verification_url == 'https://auth.example/codex/device'
        assert login.user_code == 'ABCD'
        assert 'device-secret' not in repr(login)
        await login.wait()
        assert store.value == make_codex_tokens()
        with pytest.raises(CodexAuthError, match='not pending'):
            await login.wait()
        await auth.logout()

    assert store.value is None
    assert json_body(requests[0]) == {'client_id': config.client_id}
    assert json_body(requests[1]) == {'device_auth_id': 'device-secret', 'user_code': 'ABCD'}
    assert b'code-secret' in requests[-1].content


@pytest.mark.asyncio
async def test_browser_login_validates_state_exchanges_and_persists_tokens() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)

        return codex_token_response()

    store = MemoryTokenStore()
    config = CodexOAuthConfig(issuer='https://auth.example', callback_ports=(0,), login_timeout_seconds=1)
    async with oauth_client(handler) as client, CodexAuth(store=store, http_client=client, config=config) as auth:
        login = await auth.start_browser_login()
        authorization = urlsplit(login.authorization_url)
        query = parse_qs(authorization.query)
        redirect_uri = query['redirect_uri'][0]
        state = query['state'][0]
        assert authorization.path == '/oauth/authorize'
        assert query['code_challenge_method'] == ['S256']
        assert len(query['code_challenge'][0]) == 43

        wait_task = asyncio.create_task(login.wait())
        async with httpx.AsyncClient(trust_env=False) as callback_client:
            rejected = await callback_client.get(redirect_uri, params={'code': 'secret', 'state': 'wrong'})
            callback_task = asyncio.create_task(
                callback_client.get(redirect_uri, params={'code': 'browser-code', 'state': state})
            )
            await wait_task
            callback_response = await callback_task

        assert rejected.status_code == 400
        assert callback_response.status_code == 200
        assert store.value == make_codex_tokens()
        with pytest.raises(CodexAuthError, match='not pending'):
            await login.wait()

    assert requests[0].url.path == '/oauth/token'
    assert b'browser-code' in requests[0].content
    assert b'http%3A%2F%2Flocalhost%3A' in requests[0].content


@pytest.mark.asyncio
async def test_login_attempts_can_be_cancelled_and_do_not_overlap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith('/deviceauth/usercode'):
            return httpx.Response(200, json={'device_auth_id': 'id', 'user_code': 'CODE', 'interval': 60})

        return httpx.Response(403)

    async with oauth_client(handler) as client, CodexAuth.ephemeral(http_client=client) as auth:
        login = await auth.start_device_login()
        with pytest.raises(CodexAuthError, match='already in progress'):
            await auth.start_browser_login()

        wait_task = asyncio.create_task(login.wait())
        await asyncio.sleep(0)
        await login.cancel()
        with pytest.raises(CodexAuthError, match='cancelled'):
            await wait_task
        await login.cancel()

        browser = await auth.start_browser_login()
        browser_wait = asyncio.create_task(browser.wait())
        await asyncio.sleep(0)
        await browser.cancel()
        with pytest.raises(CodexAuthError, match='cancelled'):
            await browser_wait

        pending = await auth.start_device_login()
        await pending.cancel()


@pytest.mark.asyncio
async def test_auth_context_owns_default_client_and_rejects_invalid_lifecycle() -> None:
    auth = CodexAuth.ephemeral(config=CodexOAuthConfig(callback_ports=(0,)))
    assert auth.config.callback_ports == (0,)
    with pytest.raises(CodexAuthError, match='not active'):
        await auth.logout()

    async with auth:
        with pytest.raises(CodexAuthError, match='already active'):
            await auth.__aenter__()

    assert auth._client.is_closed
    with pytest.raises(CodexAuthError, match='closed'):
        await auth.__aenter__()
