import pytest
from keyring.errors import KeyringError
from pytest_mock import MockerFixture

from ovid_core import CodexAuthError
from ovid_core.codex import CodexAuth, KeyringCodexTokenStore
from tests.support.helpers import make_codex_tokens


@pytest.mark.asyncio
async def test_persistent_auth_uses_keyring_store(mocker: MockerFixture) -> None:
    get_password = mocker.patch('keyring.get_password', return_value=None)

    async with CodexAuth.persistent(service='test', account='user') as auth:
        with pytest.raises(CodexAuthError, match='required'):
            await auth._request_tokens()

    get_password.assert_called_once_with('test', 'user')


@pytest.mark.asyncio
async def test_keyring_store_round_trip(mocker: MockerFixture) -> None:
    values: dict[tuple[str, str], str] = {}
    mocker.patch('keyring.get_password', side_effect=lambda service, account: values.get((service, account)))
    mocker.patch(
        'keyring.set_password',
        side_effect=lambda service, account, value: values.__setitem__((service, account), value),
    )
    mocker.patch('keyring.delete_password', side_effect=lambda service, account: values.pop((service, account)))
    store = KeyringCodexTokenStore(service='test', account='user')

    assert await store.load() is None
    await store.save(make_codex_tokens())
    assert await store.load() == make_codex_tokens()
    await store.delete()
    assert await store.load() is None
    await store.delete()


@pytest.mark.asyncio
async def test_keyring_failures_are_redacted(mocker: MockerFixture) -> None:
    store = KeyringCodexTokenStore(service='test', account='account')
    get_password = mocker.patch('keyring.get_password', return_value='{"id_token":"backend-secret"}')
    with pytest.raises(CodexAuthError) as load_error:
        await store.load()

    def fail(*_args: str) -> None:
        raise KeyringError('backend-secret')

    mocker.patch('keyring.set_password', side_effect=fail)
    with pytest.raises(CodexAuthError) as save_error:
        await store.save(make_codex_tokens())
    get_password.return_value = 'stored'
    mocker.patch('keyring.delete_password', side_effect=fail)
    with pytest.raises(CodexAuthError) as delete_error:
        await store.delete()

    assert 'backend-secret' not in repr(load_error.value)
    assert 'backend-secret' not in repr(save_error.value)
    assert 'backend-secret' not in repr(delete_error.value)
