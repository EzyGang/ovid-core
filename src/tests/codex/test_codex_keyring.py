import asyncio
import json
from pathlib import Path
from threading import Event
from traceback import format_exception

import pytest
from filelock import Timeout
from keyring.errors import KeyringError
from pytest_mock import MockerFixture

from ovid_core import CodexAuthError
from ovid_core.codex import CodexAuth, KeyringCodexTokenStore
from tests.support.helpers import make_codex_tokens


@pytest.fixture(autouse=True)
def keyring_home(mocker: MockerFixture, tmp_path: Path) -> None:
    mocker.patch('ovid_core.codex.keyring.Path.home', return_value=tmp_path)


@pytest.fixture
def keyring_values(mocker: MockerFixture) -> dict[tuple[str, str], str]:
    values: dict[tuple[str, str], str] = {}
    mocker.patch('keyring.get_password', side_effect=lambda service, account: values.get((service, account)))
    mocker.patch(
        'keyring.set_password',
        side_effect=lambda service, account, value: values.__setitem__((service, account), value),
    )
    return values


@pytest.mark.asyncio
async def test_persistent_auth_uses_keyring_store(keyring_values: dict[tuple[str, str], str]) -> None:
    async with CodexAuth.persistent(service='test', account='user') as auth:
        with pytest.raises(CodexAuthError):
            await auth._request_tokens()


@pytest.mark.asyncio
async def test_keyring_revisions_survive_replacement_and_deletion(
    keyring_values: dict[tuple[str, str], str],
) -> None:
    store = KeyringCodexTokenStore(service='test', account='user')
    other = KeyringCodexTokenStore(service='test', account='user')
    initial = await store.snapshot()
    assert initial.tokens is None
    tokens = make_codex_tokens()
    await store.save(tokens)
    saved = await other.snapshot()
    assert saved.tokens == tokens
    assert saved.revision > initial.revision

    replacement = make_codex_tokens(suffix='replacement')
    assert await other.compare_and_swap(saved.revision, replacement)
    replaced = await store.snapshot()
    assert replaced.tokens == replacement
    assert replaced.revision > saved.revision
    assert not await store.compare_and_swap(saved.revision, tokens)
    assert await other.snapshot() == replaced

    await store.delete()
    deleted = await other.snapshot()
    assert deleted.tokens is None
    assert deleted.revision > replaced.revision
    assert not await other.compare_and_swap(replaced.revision, tokens)
    assert await store.snapshot() == deleted
    await other.delete()
    assert (await store.snapshot()).revision > deleted.revision
    await store.save(tokens)
    assert await other.load() == tokens
    assert (await other.snapshot()).revision > deleted.revision


@pytest.mark.asyncio
async def test_keyring_conditional_writers_share_account_lock(
    keyring_values: dict[tuple[str, str], str],
) -> None:
    first = KeyringCodexTokenStore(service='test', account='user')
    second = KeyringCodexTokenStore(service='test', account='user')
    initial = await first.snapshot()
    candidates = [make_codex_tokens(suffix='first'), make_codex_tokens(suffix='second')]
    results = await asyncio.gather(
        first.compare_and_swap(initial.revision, candidates[0]),
        second.compare_and_swap(initial.revision, candidates[1]),
    )

    assert results.count(True) == 1
    assert await first.load() == candidates[results.index(True)]


@pytest.mark.asyncio
async def test_keyring_reads_and_replaces_legacy_credentials(keyring_values: dict[tuple[str, str], str]) -> None:
    tokens = make_codex_tokens()
    keyring_values['test', 'user'] = json.dumps(
        {
            'id_token': tokens.id_token.get_secret_value(),
            'access_token': tokens.access_token.get_secret_value(),
            'refresh_token': tokens.refresh_token.get_secret_value(),
        }
    )
    store = KeyringCodexTokenStore(service='test', account='user')
    legacy = await store.snapshot()
    assert legacy.tokens == tokens
    replacement = make_codex_tokens(suffix='replacement')
    assert await store.compare_and_swap(legacy.revision, replacement)
    assert await store.load() == replacement
    assert (await store.snapshot()).revision > legacy.revision


@pytest.mark.asyncio
async def test_failed_keyring_write_preserves_snapshot(
    mocker: MockerFixture,
    keyring_values: dict[tuple[str, str], str],
) -> None:
    store = KeyringCodexTokenStore(service='test', account='user')
    await store.save(make_codex_tokens())
    before = await store.snapshot()
    mocker.patch('keyring.set_password', side_effect=KeyringError('backend-secret'))

    with pytest.raises(CodexAuthError):
        await store.compare_and_swap(before.revision, make_codex_tokens(suffix='replacement'))
    assert await store.snapshot() == before
    with pytest.raises(CodexAuthError):
        await store.delete()
    assert await store.snapshot() == before


@pytest.mark.asyncio
async def test_keyring_failures_are_redacted(mocker: MockerFixture) -> None:
    store = KeyringCodexTokenStore(service='test', account='account')
    get_password = mocker.patch('keyring.get_password', return_value='{"id_token":"backend-secret"}')
    with pytest.raises(CodexAuthError) as invalid_error:
        await store.load()
    get_password.side_effect = KeyringError('backend-secret')
    with pytest.raises(CodexAuthError) as read_error:
        await store.snapshot()
    get_password.side_effect = None
    get_password.return_value = None
    mocker.patch('keyring.set_password', side_effect=KeyringError('backend-secret'))
    with pytest.raises(CodexAuthError) as save_error:
        await store.save(make_codex_tokens())
    with pytest.raises(CodexAuthError) as delete_error:
        await store.delete()
    mocker.patch('ovid_core.codex.keyring.FileLock', side_effect=Timeout('backend-secret'))
    with pytest.raises(CodexAuthError) as lock_error:
        await store.snapshot()

    for error in (invalid_error, read_error, save_error, delete_error, lock_error):
        assert 'backend-secret' not in ''.join(format_exception(error.value))


async def test_cancelled_keyring_write_finishes_before_cancellation_returns(
    mocker: MockerFixture,
    keyring_values: dict[tuple[str, str], str],
) -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = Event()

    def save(service: str, account: str, value: str) -> None:
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        keyring_values[service, account] = value

    mocker.patch('keyring.set_password', side_effect=save)
    store = KeyringCodexTokenStore(service='cancel', account='user')
    writing = asyncio.create_task(store.save(make_codex_tokens()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        writing.cancel()
        await asyncio.sleep(0)
        assert not writing.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await writing
    await store.delete()
    assert await store.load() is None
