import asyncio
from collections.abc import Awaitable, Callable
from typing import cast
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pytest_mock import MockerFixture

from ovid_core import CodexAuthError
from ovid_core.codex import CodexAuth, CodexOAuthConfig
from tests.support.helpers import codex_token_response, oauth_client


async def browser_callback(
    login_url: str,
    callback: Callable[[str, str], Awaitable[httpx.Response]],
) -> httpx.Response:
    authorization = urlsplit(login_url)
    query = parse_qs(authorization.query)
    redirect = urlsplit(query['redirect_uri'][0])
    callback_url = redirect._replace(netloc=f'127.0.0.1:{redirect.port}').geturl()

    return await callback(callback_url, query['state'][0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('params', 'message'),
    [
        ({'error': 'access_denied'}, 'rejected'),
        ({}, 'authorization code'),
    ],
)
async def test_browser_callback_failures_are_terminal(params: dict[str, str], message: str) -> None:
    async with (
        oauth_client(lambda request: codex_token_response()) as client,
        CodexAuth.ephemeral(
            http_client=client, config=CodexOAuthConfig(callback_ports=(0,), login_timeout_seconds=1)
        ) as auth,
    ):
        login = await auth.start_browser_login()
        wait_task = asyncio.create_task(login.wait())

        async def submit(redirect_uri: str, state: str) -> httpx.Response:
            async with httpx.AsyncClient(trust_env=False) as callback_client:
                return await callback_client.get(redirect_uri, params={'state': state, **params})

        response = await browser_callback(login.authorization_url, submit)
        with pytest.raises(CodexAuthError, match=message):
            await wait_task

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_browser_login_times_out() -> None:
    config = CodexOAuthConfig(callback_ports=(0,), login_timeout_seconds=0.001)
    async with (
        oauth_client(lambda request: codex_token_response()) as client,
        CodexAuth.ephemeral(http_client=client, config=config) as auth,
    ):
        login = await auth.start_browser_login()
        with pytest.raises(CodexAuthError, match='timed out'):
            await login.wait()


@pytest.mark.asyncio
async def test_browser_login_timeout_bounds_stalled_token_exchange() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError('The timed out request resumed')

    config = CodexOAuthConfig(callback_ports=(0,), login_timeout_seconds=0.05)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None) as client,
        CodexAuth.ephemeral(http_client=client, config=config) as auth,
    ):
        login = await auth.start_browser_login()
        wait_task = asyncio.create_task(login.wait())

        async def submit(redirect_uri: str, state: str) -> httpx.Response:
            async with httpx.AsyncClient(trust_env=False) as callback_client:
                return await callback_client.get(redirect_uri, params={'code': 'code', 'state': state})

        callback_task = asyncio.create_task(browser_callback(login.authorization_url, submit))
        with pytest.raises(CodexAuthError, match='timed out'):
            await wait_task
        response = await callback_task

    assert response.status_code == 500


@pytest.mark.asyncio
async def test_browser_callback_rejects_invalid_http_and_paths() -> None:
    config = CodexOAuthConfig(callback_ports=(0,))
    async with (
        oauth_client(lambda request: codex_token_response()) as client,
        CodexAuth.ephemeral(http_client=client, config=config) as auth,
    ):
        login = await auth.start_browser_login()
        authorization = urlsplit(login.authorization_url)
        query = parse_qs(authorization.query)
        redirect = urlsplit(query['redirect_uri'][0])
        callback_url = redirect._replace(netloc=f'127.0.0.1:{redirect.port}').geturl()

        async with httpx.AsyncClient(trust_env=False) as callback_client:
            missing = await callback_client.get(f'http://127.0.0.1:{redirect.port}/wrong')
            missing_state = await callback_client.get(callback_url, params={'code': 'code'})

        reader, writer = await asyncio.open_connection('127.0.0.1', cast(int, redirect.port))
        writer.write(b'POST /auth/callback HTTP/1.1\r\nHost: localhost\r\n\r\n')
        await writer.drain()
        invalid_http = await reader.read()
        writer.close()
        await writer.wait_closed()
        completed = asyncio.get_running_loop().create_future()
        completed.set_result((200, 'complete'))
        login._callback._pending.add(completed)
        await login.cancel()
        await login.cancel()

    assert missing.status_code == 404
    assert missing_state.status_code == 400
    assert b'400 Bad Request' in invalid_http


@pytest.mark.asyncio
async def test_browser_callback_is_released_when_login_is_cancelled() -> None:
    config = CodexOAuthConfig(callback_ports=(0,))
    async with (
        oauth_client(lambda request: codex_token_response()) as client,
        CodexAuth.ephemeral(http_client=client, config=config) as auth,
    ):
        login = await auth.start_browser_login()
        query = parse_qs(urlsplit(login.authorization_url).query)
        target = f'/auth/callback?code=code&state={query["state"][0]}'
        callback_task = asyncio.create_task(login._callback._process_target(target))
        await asyncio.sleep(0)
        await login.cancel()
        status, _ = await callback_task

    assert status == 503


@pytest.mark.asyncio
async def test_browser_exchange_failure_updates_callback() -> None:
    config = CodexOAuthConfig(callback_ports=(0,))
    async with (
        oauth_client(lambda request: httpx.Response(500)) as client,
        CodexAuth.ephemeral(http_client=client, config=config) as auth,
    ):
        login = await auth.start_browser_login()
        wait_task = asyncio.create_task(login.wait())

        async def submit(redirect_uri: str, state: str) -> httpx.Response:
            async with httpx.AsyncClient(trust_env=False) as callback_client:
                return await callback_client.get(redirect_uri, params={'code': 'code', 'state': state})

        callback_task = asyncio.create_task(browser_callback(login.authorization_url, submit))
        with pytest.raises(CodexAuthError, match='status 500'):
            await wait_task
        response = await callback_task

    assert response.status_code == 500


@pytest.mark.asyncio
async def test_browser_callback_uses_fallback_port_and_reports_exhaustion() -> None:
    async def ignore(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    occupied = await asyncio.start_server(ignore, host='127.0.0.1', port=0)
    port = cast(tuple[str, int], occupied.sockets[0].getsockname())[1]
    try:
        async with CodexAuth.ephemeral(config=CodexOAuthConfig(callback_ports=(port, 0))) as auth:
            login = await auth.start_browser_login()
            assert urlsplit(parse_qs(urlsplit(login.authorization_url).query)['redirect_uri'][0]).port != port
            await login.cancel()

        async with CodexAuth.ephemeral(config=CodexOAuthConfig(callback_ports=(port,))) as auth:
            with pytest.raises(CodexAuthError, match='callback server'):
                await auth.start_browser_login()
    finally:
        occupied.close()
        await occupied.wait_closed()


@pytest.mark.asyncio
async def test_external_task_cancellation_cleans_up_logins(mocker: MockerFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith('/deviceauth/usercode'):
            return httpx.Response(200, json={'device_auth_id': 'id', 'user_code': 'CODE', 'interval': 60})

        return httpx.Response(403)

    async with oauth_client(handler) as client, CodexAuth.ephemeral(http_client=client) as auth:
        device = await auth.start_device_login()
        device_wait = asyncio.create_task(device.wait())
        await asyncio.sleep(0)
        device_wait.cancel()
        with pytest.raises(asyncio.CancelledError):
            await device_wait

        browser = await auth.start_browser_login()
        browser_wait = asyncio.create_task(browser.wait())
        await asyncio.sleep(0)
        browser_wait.cancel()
        with pytest.raises(asyncio.CancelledError):
            await browser_wait

        active = await auth.start_browser_login()
        cancel = mocker.spy(active, 'cancel')

    cancel.assert_awaited_once()
