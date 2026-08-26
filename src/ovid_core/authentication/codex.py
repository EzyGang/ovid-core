import asyncio
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import SecretStr

from ovid_core.authentication.contracts import AuthenticationFlowSession, CredentialStore
from ovid_core.authentication.models import (
    AuthenticationInteraction,
    BrowserAuthorizationAuthenticationInteraction,
    CompletedAuthenticationInteraction,
    DeviceAuthorizationAuthenticationInteraction,
    FailedAuthenticationInteraction,
)
from ovid_core.codex import CodexAuth, CodexBrowserLogin, CodexDeviceLogin, CodexTokens
from ovid_core.errors import AuthenticationError, CodexAuthError


class CodexCredentialBinding:
    def __init__(self, *, auth: CodexAuth, store: CredentialStore[CodexTokens]) -> None:
        self._auth = auth
        self._store = store

    async def connected(self) -> bool:
        return await self._store.load() is not None

    async def disconnect(self) -> None:
        await self._auth.logout()


class CodexBrowserAuthenticationFlow:
    id: str = 'browser'
    label: str = 'Browser callback'

    def __init__(self, *, auth: CodexAuth, http_client: httpx.AsyncClient) -> None:
        self._auth = auth
        self._http_client = http_client

    async def create_session(self) -> AuthenticationFlowSession:
        return _CodexBrowserAuthenticationSession(auth=self._auth, http_client=self._http_client)


class CodexDeviceAuthenticationFlow:
    id: str = 'device'
    label: str = 'Device code'

    def __init__(self, *, auth: CodexAuth) -> None:
        self._auth = auth

    async def create_session(self) -> AuthenticationFlowSession:
        return _CodexDeviceAuthenticationSession(auth=self._auth)


class _CodexBrowserAuthenticationSession:
    def __init__(self, *, auth: CodexAuth, http_client: httpx.AsyncClient) -> None:
        self._auth = auth
        self._http_client = http_client
        self._login: CodexBrowserLogin | None = None
        self._pending: tuple[str, asyncio.Task[None]] | None = None

    async def start(self) -> AuthenticationInteraction:
        if self._login is not None:
            raise AuthenticationError('Authentication session already started')
        login = await self._auth.start_browser_login()
        self._login = login
        task = asyncio.create_task(login.wait())
        self._pending = (_redirect_uri(login.authorization_url), task)
        return BrowserAuthorizationAuthenticationInteraction(
            url=login.authorization_url,
            manual_callback=True,
        )

    async def poll(self) -> AuthenticationInteraction | None:
        task = None if self._pending is None else self._pending[1]
        return _task_interaction(task)

    async def submit(self, value: SecretStr) -> AuthenticationInteraction:
        pending = self._pending
        if pending is None:
            raise AuthenticationError('Authentication session is not waiting for input')
        redirect_uri, task = pending
        callback_url = value.get_secret_value()
        _validate_callback_url(callback_url, redirect_uri)
        response = await self._http_client.get(callback_url)
        if response.status_code != 200:
            raise AuthenticationError('Codex browser callback was rejected')
        await task
        return CompletedAuthenticationInteraction()

    async def cancel(self) -> None:
        await _cancel_login(self._login, None if self._pending is None else self._pending[1])


class _CodexDeviceAuthenticationSession:
    def __init__(self, *, auth: CodexAuth) -> None:
        self._auth = auth
        self._login: CodexDeviceLogin | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> AuthenticationInteraction:
        if self._login is not None:
            raise AuthenticationError('Authentication session already started')
        login = await self._auth.start_device_login()
        self._login = login
        self._task = asyncio.create_task(login.wait())
        return DeviceAuthorizationAuthenticationInteraction(
            url=login.verification_url,
            user_code=login.user_code,
        )

    async def poll(self) -> AuthenticationInteraction | None:
        return _task_interaction(self._task)

    async def submit(self, value: SecretStr) -> AuthenticationInteraction:
        raise AuthenticationError('Device authentication does not accept input')

    async def cancel(self) -> None:
        await _cancel_login(self._login, self._task)


def _task_interaction(task: asyncio.Task[None] | None) -> AuthenticationInteraction | None:
    if task is None or not task.done():
        return None
    if task.cancelled():
        return FailedAuthenticationInteraction(reason='cancelled')
    if task.exception() is not None:
        return FailedAuthenticationInteraction(reason='provider_error')
    return CompletedAuthenticationInteraction()


async def _cancel_login(
    login: CodexBrowserLogin | CodexDeviceLogin | None,
    task: asyncio.Task[None] | None,
) -> None:
    if login is not None:
        await login.cancel()
    if task is not None:
        try:
            await task
        except CodexAuthError:
            return


def _redirect_uri(authorization_url: str) -> str:
    values = parse_qs(urlsplit(authorization_url).query).get('redirect_uri')
    if not values or not values[0]:
        raise CodexAuthError('Codex browser login did not provide a callback address')
    return values[0]


def _validate_callback_url(callback_url: str, redirect_uri: str) -> None:
    callback = urlsplit(callback_url)
    expected = urlsplit(redirect_uri)
    if (
        callback.scheme != expected.scheme
        or callback.netloc != expected.netloc
        or callback.path != expected.path
        or not callback.query
        or callback.fragment
    ):
        raise CodexAuthError('Codex callback URL does not match the pending login')
