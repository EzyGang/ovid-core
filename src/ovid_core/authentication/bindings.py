from pydantic import SecretStr

from ovid_core.authentication.contracts import CredentialStore


class StoredCredentialBinding[Credential]:
    def __init__(self, store: CredentialStore[Credential]) -> None:
        self._store = store

    async def connected(self) -> bool:
        return await self._store.load() is not None

    async def disconnect(self) -> None:
        await self._store.delete()


class StoredAPIKeyBinding(StoredCredentialBinding[SecretStr]):
    def __init__(self, store: CredentialStore[SecretStr]) -> None:
        super().__init__(store)
        self._api_key_store = store

    async def save_api_key(self, api_key: SecretStr) -> None:
        if not api_key.get_secret_value():
            raise ValueError('API key must not be empty')
        await self._api_key_store.save(api_key)
