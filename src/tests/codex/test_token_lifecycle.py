import asyncio

import httpx
import pytest
from pytest_mock import MockerFixture

from ovid_core.codex import CodexAuth, CodexOAuthConfig, CodexTokens, MemoryCodexTokenStore
from ovid_core.errors import CodexAuthError
from tests.support.helpers import codex_token_response, make_codex_tokens


@pytest.mark.asyncio
async def test_store_revisions_prevent_delete_recreate_aba() -> None:
    store = MemoryCodexTokenStore()
    initial = await store.snapshot()
    tokens = make_codex_tokens()
    assert await store.compare_and_swap(initial.revision, tokens)
    saved = await store.snapshot()
    await store.delete()
    deleted = await store.snapshot()
    assert deleted.tokens is None
    assert deleted.revision > saved.revision
    assert not await store.compare_and_swap(saved.revision, tokens)
    assert await store.snapshot() == deleted
    assert await store.compare_and_swap(deleted.revision, tokens)
    assert not await store.compare_and_swap(initial.revision, tokens)
    assert (await store.snapshot()).tokens == tokens


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['replace', 'delete', 'expired'])
async def test_late_refresh_honors_replacement_or_deletion(change: str) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return codex_token_response()

    store = MemoryCodexTokenStore()
    await store.save(make_codex_tokens(expired=True))
    replacement = make_codex_tokens(suffix='replacement', expired=change == 'expired')
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        CodexAuth(store=store, http_client=client) as auth,
    ):
        pending = asyncio.create_task(auth._request_tokens())
        await started.wait()
        if change == 'delete':
            await auth.logout()
        else:
            await store.save(replacement)
        current = await store.snapshot()
        release.set()
        if change != 'replace':
            with pytest.raises(CodexAuthError):
                await pending
        else:
            assert await pending == replacement
        assert await store.snapshot() == current


@pytest.mark.asyncio
async def test_token_requests_observe_external_changes_without_refresh() -> None:
    store = MemoryCodexTokenStore()
    original = make_codex_tokens()
    replacement = make_codex_tokens(suffix='replacement')
    await store.save(original)
    async with CodexAuth(store=store) as auth:
        assert await auth._request_tokens() == original
        await store.save(replacement)
        assert await auth._request_tokens() == replacement
        await store.delete()
        with pytest.raises(CodexAuthError):
            await auth._request_tokens()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_late_device_authorization_does_not_persist_superseded_tokens(cancel: bool) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith('/deviceauth/usercode'):
            return httpx.Response(200, json={'device_auth_id': 'id', 'user_code': 'CODE', 'interval': '0.001'})
        if request.url.path.endswith('/deviceauth/token'):
            return httpx.Response(200, json={'authorization_code': 'code', 'code_verifier': 'verifier'})
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return codex_token_response()

    store = MemoryCodexTokenStore()
    original = make_codex_tokens(suffix='original')
    await store.save(original)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        CodexAuth(store=store, http_client=client, config=CodexOAuthConfig(login_timeout_seconds=30)) as auth,
    ):
        login = await auth.start_device_login()
        waiting = asyncio.create_task(login.wait())
        await started.wait()
        if cancel:
            stopping = asyncio.create_task(login.cancel())
            await cancelled.wait()
        else:
            await store.delete()
        expected = await store.snapshot()
        release.set()
        with pytest.raises(CodexAuthError):
            await waiting
        if cancel:
            await stopping
            next_login = await auth.start_device_login()
            await next_login.cancel()
        assert await store.snapshot() == expected


async def test_login_cancellation_waits_for_an_admitted_commit(mocker: MockerFixture) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    store = MemoryCodexTokenStore()
    compare_and_swap = store.compare_and_swap

    async def commit(revision: int, tokens: CodexTokens) -> bool:
        entered.set()
        await release.wait()
        return await compare_and_swap(revision, tokens)

    def backend(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith('/deviceauth/usercode'):
            return httpx.Response(200, json={'device_auth_id': 'id', 'user_code': 'CODE', 'interval': '0.001'})
        if request.url.path.endswith('/deviceauth/token'):
            return httpx.Response(200, json={'authorization_code': 'code', 'code_verifier': 'verifier'})
        return codex_token_response()

    mocker.patch.object(store, 'compare_and_swap', side_effect=commit)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(backend)) as client,
        CodexAuth(store=store, http_client=client) as auth,
    ):
        login = await auth.start_device_login()
        waiting = asyncio.create_task(login.wait())
        await asyncio.wait_for(entered.wait(), 2)
        stopping = asyncio.create_task(login.cancel())
        await asyncio.sleep(0)
        assert not stopping.done()
        release.set()
        await stopping
        with pytest.raises(CodexAuthError):
            await waiting
        await auth.logout()
        assert (await store.snapshot()).tokens is None
