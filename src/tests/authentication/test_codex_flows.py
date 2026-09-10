import asyncio
from typing import cast
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from pydantic import SecretStr
from pytest_mock import MockerFixture

from ovid_core.authentication import (
    AuthenticationFlowSession,
    AuthenticationInteraction,
    BrowserAuthorizationAuthenticationInteraction,
    CodexBrowserAuthenticationFlow,
    CodexCredentialBinding,
    CodexDeviceAuthenticationFlow,
    CompletedAuthenticationInteraction,
    DeviceAuthorizationAuthenticationInteraction,
    FailedAuthenticationInteraction,
)
from ovid_core.authentication.codex import _task_interaction
from ovid_core.codex import CodexAuth, CodexOAuthConfig, MemoryCodexTokenStore
from ovid_core.errors import AuthenticationError, CodexAuthError
from tests.support.helpers import codex_token_response, oauth_client


async def test_device_flow_reports_generic_interactions_and_persists() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith('/deviceauth/usercode'):
            return httpx.Response(200, json={'device_auth_id': 'device', 'user_code': 'CODE', 'interval': 0.001})
        if request.url.path.endswith('/deviceauth/token'):
            return httpx.Response(200, json={'authorization_code': 'authorization', 'code_verifier': 'verifier'})
        return codex_token_response()

    store = MemoryCodexTokenStore()
    config = CodexOAuthConfig(issuer='https://auth.example', login_timeout_seconds=1)
    async with oauth_client(handler) as client, CodexAuth(store=store, http_client=client, config=config) as auth:
        binding = CodexCredentialBinding(auth=auth, store=store)
        flow = CodexDeviceAuthenticationFlow(auth=auth)
        assert flow.id == 'device'
        unstarted = await flow.create_session()
        assert await unstarted.poll() is None
        await unstarted.cancel()
        session = await flow.create_session()
        interaction = await session.start()
        assert interaction == DeviceAuthorizationAuthenticationInteraction(
            url='https://auth.example/codex/device',
            user_code='CODE',
        )
        assert await session.poll() is None
        with pytest.raises(AuthenticationError, match='already started'):
            await session.start()
        with pytest.raises(AuthenticationError, match='does not accept'):
            await session.submit(SecretStr('callback'))
        completed = await wait_for_completion(session)
        assert completed == CompletedAuthenticationInteraction()
        assert await binding.connected()
        await binding.disconnect()
        assert not await binding.connected()


async def test_browser_flow_accepts_remote_callback_and_cancels() -> None:
    store = MemoryCodexTokenStore()
    config = CodexOAuthConfig(
        issuer='https://auth.example',
        callback_ports=(0,),
        login_timeout_seconds=1,
    )
    async with (
        oauth_client(lambda _request: codex_token_response()) as auth_client,
        httpx.AsyncClient(trust_env=False) as callback_client,
        CodexAuth(store=store, http_client=auth_client, config=config) as auth,
    ):
        flow = CodexBrowserAuthenticationFlow(auth=auth, http_client=callback_client)
        assert flow.id == 'browser'
        session = await flow.create_session()
        interaction = await session.start()
        assert isinstance(interaction, BrowserAuthorizationAuthenticationInteraction)
        assert interaction.manual_callback
        query = parse_qs(urlsplit(interaction.url).query)
        callback_url = f'{query["redirect_uri"][0]}?{urlencode({"code": "code", "state": query["state"][0]})}'
        completed = await session.submit(SecretStr(callback_url))
        assert completed == CompletedAuthenticationInteraction()

        cancelled = await flow.create_session()
        await cancelled.start()
        await cancelled.cancel()


async def test_browser_flow_rejects_invalid_callbacks_and_failed_logins() -> None:
    store = MemoryCodexTokenStore()
    config = CodexOAuthConfig(issuer='https://auth.example', callback_ports=(0,), login_timeout_seconds=5)
    async with (
        oauth_client(lambda _request: httpx.Response(500)) as auth_client,
        httpx.AsyncClient(trust_env=False) as callback_client,
        CodexAuth(store=store, http_client=auth_client, config=config) as auth,
    ):
        flow = CodexBrowserAuthenticationFlow(auth=auth, http_client=callback_client)
        unstarted = await flow.create_session()
        assert await unstarted.poll() is None
        with pytest.raises(AuthenticationError, match='not waiting'):
            await unstarted.submit(SecretStr('http://localhost/callback?code=x'))

        session = await flow.create_session()
        interaction = await session.start()
        with pytest.raises(AuthenticationError, match='already started'):
            await session.start()
        query = parse_qs(urlsplit(interaction.url).query)
        rejected_url = f'{query["redirect_uri"][0]}?code=x'
        with pytest.raises(AuthenticationError, match='callback was rejected'):
            await session.submit(SecretStr(rejected_url))
        with pytest.raises(CodexAuthError, match='does not match'):
            await session.submit(SecretStr('https://example.com/callback?code=x'))
        denied_url = f'{query["redirect_uri"][0]}?{urlencode({"error": "access_denied", "state": query["state"][0]})}'
        with pytest.raises(AuthenticationError, match='callback was rejected'):
            await session.submit(SecretStr(denied_url))
        failed = await wait_for_completion(session)
        assert failed == FailedAuthenticationInteraction(reason='provider_error')


async def test_browser_flow_rejects_missing_callback_address(mocker: MockerFixture) -> None:
    login = mocker.Mock()
    login.authorization_url = 'https://auth.example/authorize'
    login.wait = mocker.AsyncMock()
    auth = cast(CodexAuth, mocker.Mock())
    auth.start_browser_login = mocker.AsyncMock(return_value=login)
    flow = CodexBrowserAuthenticationFlow(auth=auth, http_client=mocker.Mock())

    with pytest.raises(CodexAuthError, match='callback address'):
        await (await flow.create_session()).start()


async def test_cancelled_task_maps_to_semantic_failure() -> None:
    task = asyncio.create_task(asyncio.sleep(1))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _task_interaction(task) == FailedAuthenticationInteraction(reason='cancelled')


async def wait_for_completion(session: AuthenticationFlowSession) -> AuthenticationInteraction:
    for _attempt in range(100):
        interaction = await session.poll()
        if interaction is not None:
            return interaction
        await asyncio.sleep(0.005)
    raise AssertionError('authentication flow did not finish')
