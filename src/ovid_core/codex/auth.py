from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Self, cast

import httpx

from ovid_core.codex._browser import BrowserCallbackServer
from ovid_core.codex._device import _DeviceAuthorization, _DeviceLoginFlow
from ovid_core.codex._oauth import exchange_authorization_code
from ovid_core.codex.keyring import KeyringCodexTokenStore
from ovid_core.codex.models import CodexOAuthConfig, CodexTokens
from ovid_core.codex.tokens import CodexTokenStore, MemoryCodexTokenStore, _CodexTokenManager
from ovid_core.errors import CodexAuthError


class CodexAuth:
    def __init__(
        self,
        *,
        store: CodexTokenStore,
        http_client: httpx.AsyncClient | None = None,
        config: CodexOAuthConfig | None = None,
    ) -> None:
        self._config = config or CodexOAuthConfig()
        self._client = http_client or httpx.AsyncClient()
        self._owns_client = http_client is None
        self._tokens = _CodexTokenManager(store=store, http_client=self._client, config=self._config)
        self._login: CodexBrowserLogin | CodexDeviceLogin | None = None
        self._login_lock = asyncio.Lock()
        self._active = False
        self._closed = False

    @classmethod
    def persistent(
        cls,
        *,
        http_client: httpx.AsyncClient | None = None,
        config: CodexOAuthConfig | None = None,
        service: str = 'ovid-core.codex',
        account: str = 'default',
    ) -> Self:
        return cls(
            store=KeyringCodexTokenStore(service=service, account=account),
            http_client=http_client,
            config=config,
        )

    @classmethod
    def ephemeral(cls, *, http_client: httpx.AsyncClient | None = None, config: CodexOAuthConfig | None = None) -> Self:
        return cls(store=MemoryCodexTokenStore(), http_client=http_client, config=config)

    @property
    def config(self) -> CodexOAuthConfig:
        return self._config

    async def __aenter__(self) -> Self:
        if self._closed:
            raise CodexAuthError('Codex authentication service is closed')
        if self._active:
            raise CodexAuthError('Codex authentication service is already active')

        self._active = True

        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        try:
            login = self._login
            if login is not None:
                await login.cancel()
        finally:
            self._active = False
            self._closed = True
            if self._owns_client:
                await self._client.aclose()

    async def start_browser_login(self) -> CodexBrowserLogin:
        async with self._login_lock:
            self._ensure_login_available()
            callback = await BrowserCallbackServer.start(config=self._config)
            login = CodexBrowserLogin(auth=self, callback=callback)
            self._login = login

            return login

    async def start_device_login(self) -> CodexDeviceLogin:
        async with self._login_lock:
            self._ensure_login_available()
            flow = _DeviceLoginFlow(http_client=self._client, config=self._config)
            authorization = await flow.start()
            login = CodexDeviceLogin(auth=self, flow=flow, authorization=authorization)
            self._login = login

            return login

    async def logout(self) -> None:
        self._ensure_active()
        await self._tokens.logout()

    async def _request_tokens(self, *, force_refresh: bool = False) -> CodexTokens:
        self._ensure_active()

        return await self._tokens.tokens(force_refresh=force_refresh)

    async def _save(self, tokens: CodexTokens) -> None:
        self._ensure_active()
        await self._tokens.save(tokens)

    async def _release(self) -> None:
        async with self._login_lock:
            self._login = None

    def _ensure_login_available(self) -> None:
        self._ensure_active()
        if self._login is not None:
            raise CodexAuthError('A Codex login is already in progress')

    def _ensure_active(self) -> None:
        if not self._active:
            raise CodexAuthError('Codex authentication service is not active')


class CodexBrowserLogin:
    def __init__(self, *, auth: CodexAuth, callback: BrowserCallbackServer) -> None:
        self._auth = auth
        self._callback = callback
        self._wait_task: asyncio.Task[None] | None = None
        self._cancelled = False
        self._finished = False

    @property
    def authorization_url(self) -> str:
        return self._callback.authorization_url

    async def wait(self) -> None:
        self._begin_wait()
        submission = None
        try:
            submission = await self._callback.next_submission(timeout_seconds=self._auth._config.login_timeout_seconds)
            tokens = await exchange_authorization_code(
                http_client=self._auth._client,
                config=self._auth._config,
                code=submission.code,
                redirect_uri=self._callback.redirect_uri,
                code_verifier=self._callback.code_verifier,
            )
            await self._auth._save(tokens)
            submission.finish(success=True)
        except asyncio.CancelledError:
            if self._cancelled:
                raise CodexAuthError('Codex login was cancelled') from None
            raise
        except Exception:
            if submission is not None:
                submission.finish(success=False)
            raise
        finally:
            await self._finish()

    async def cancel(self) -> None:
        if self._finished:
            return

        self._cancelled = True
        task = self._wait_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        await self._finish()

    def _begin_wait(self) -> None:
        if self._finished or self._wait_task is not None:
            raise CodexAuthError('Codex login is not pending')

        self._wait_task = cast(asyncio.Task[None], asyncio.current_task())

    async def _finish(self) -> None:
        if self._finished:
            return

        self._finished = True
        try:
            await self._callback.close()
        finally:
            await self._auth._release()


class CodexDeviceLogin:
    def __init__(self, *, auth: CodexAuth, flow: _DeviceLoginFlow, authorization: _DeviceAuthorization) -> None:
        self._auth = auth
        self._flow = flow
        self._authorization = authorization
        self._cancelled = False
        self._wait_task: asyncio.Task[None] | None = None
        self._finished = False

    @property
    def verification_url(self) -> str:
        return self._authorization.verification_url

    @property
    def user_code(self) -> str:
        return self._authorization.user_code

    async def wait(self) -> None:
        self._begin_wait()
        try:
            tokens = await self._flow.complete(self._authorization)
            await self._auth._save(tokens)
        except asyncio.CancelledError:
            if self._cancelled:
                raise CodexAuthError('Codex login was cancelled') from None
            raise
        finally:
            await self._finish()

    async def cancel(self) -> None:
        if self._finished:
            return

        self._cancelled = True
        task = self._wait_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        await self._finish()

    def _begin_wait(self) -> None:
        if self._finished or self._wait_task is not None:
            raise CodexAuthError('Codex login is not pending')

        self._wait_task = cast(asyncio.Task[None], asyncio.current_task())

    async def _finish(self) -> None:
        if self._finished:
            return

        self._finished = True
        await self._auth._release()
