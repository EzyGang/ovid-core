import asyncio

import keyring
from keyring.errors import KeyringError
from pydantic import Field, SecretStr, ValidationError

from ovid_core.codex.models import CodexTokens
from ovid_core.errors import CodexAuthError
from ovid_core.models import BaseModel


class _StoredTokenPayload(BaseModel):
    id_token: str = Field(min_length=1, repr=False)
    access_token: str = Field(min_length=1, repr=False)
    refresh_token: str = Field(min_length=1, repr=False)


class KeyringCodexTokenStore:
    def __init__(self, *, service: str = 'ovid-core.codex', account: str = 'default') -> None:
        self._service = service
        self._account = account

    async def load(self) -> CodexTokens | None:
        try:
            serialized = await asyncio.to_thread(keyring.get_password, self._service, self._account)
            if serialized is None:
                return None
            payload = _StoredTokenPayload.model_validate_json(serialized)
            return CodexTokens(
                id_token=SecretStr(payload.id_token),
                access_token=SecretStr(payload.access_token),
                refresh_token=SecretStr(payload.refresh_token),
            )
        except KeyringError, ValidationError, ValueError:
            raise CodexAuthError('Codex credentials could not be loaded from the system keyring') from None

    async def save(self, tokens: CodexTokens) -> None:
        serialized = _StoredTokenPayload(
            id_token=tokens.id_token.get_secret_value(),
            access_token=tokens.access_token.get_secret_value(),
            refresh_token=tokens.refresh_token.get_secret_value(),
        ).model_dump_json()

        try:
            await asyncio.to_thread(keyring.set_password, self._service, self._account, serialized)
        except KeyringError:
            raise CodexAuthError('Codex credentials could not be saved to the system keyring') from None

    async def delete(self) -> None:
        try:
            existing = await asyncio.to_thread(keyring.get_password, self._service, self._account)
            if existing is not None:
                await asyncio.to_thread(keyring.delete_password, self._service, self._account)
        except KeyringError:
            raise CodexAuthError('Codex credentials could not be removed from the system keyring') from None
