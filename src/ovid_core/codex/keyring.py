import asyncio
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import keyring
from filelock import FileLock, Timeout
from keyring.errors import KeyringError
from pydantic import Field, SecretStr, TypeAdapter, ValidationError

from ovid_core.codex.models import CodexTokens, CodexTokenSnapshot
from ovid_core.errors import CodexAuthError
from ovid_core.models import BaseModel


class _StoredTokenPayload(BaseModel):
    id_token: str = Field(min_length=1, repr=False)
    access_token: str = Field(min_length=1, repr=False)
    refresh_token: str = Field(min_length=1, repr=False)


class _StoredSnapshot(BaseModel):
    revision: int = Field(ge=0)
    tokens: _StoredTokenPayload | None = Field(repr=False)


class KeyringCodexTokenStore:
    def __init__(self, *, service: str = 'ovid-core.codex', account: str = 'default') -> None:
        self._service = service
        self._account = account
        identity = f'{len(service)}:{service}{account}'.encode()
        self._lock_name = f'{hashlib.sha256(identity).hexdigest()}.lock'
        self._adapter = TypeAdapter(_StoredSnapshot | _StoredTokenPayload)

    async def load(self) -> CodexTokens | None:
        return (await self.snapshot()).tokens

    async def snapshot(self) -> CodexTokenSnapshot:
        return await asyncio.to_thread(self._snapshot)

    async def save(self, tokens: CodexTokens) -> None:
        await self._mutate(tokens)

    async def delete(self) -> None:
        await self._mutate(None)

    async def compare_and_swap(self, expected_revision: int, tokens: CodexTokens) -> bool:
        return await self._mutate(tokens, expected_revision)

    async def _mutate(self, tokens: CodexTokens | None, expected_revision: int | None = None) -> bool:
        task = asyncio.create_task(asyncio.to_thread(self._replace, tokens, expected_revision))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            directory = Path.home() / '.ovid-core-keyring'
            directory.mkdir(mode=0o700, exist_ok=True)
            with FileLock(directory / self._lock_name, timeout=5, mode=0o600):
                yield
        except KeyringError, ValidationError, ValueError, OSError, Timeout:
            raise CodexAuthError('Codex credentials could not be accessed in the system keyring') from None

    def _read(self) -> _StoredSnapshot:
        serialized = keyring.get_password(self._service, self._account)
        if serialized is None:
            return _StoredSnapshot(revision=0, tokens=None)
        record = self._adapter.validate_json(serialized)
        if isinstance(record, _StoredTokenPayload):
            return _StoredSnapshot(revision=0, tokens=record)
        return record

    def _snapshot(self) -> CodexTokenSnapshot:
        with self._locked():
            record = self._read()
            payload = record.tokens
            tokens = (
                None
                if payload is None
                else CodexTokens(
                    id_token=SecretStr(payload.id_token),
                    access_token=SecretStr(payload.access_token),
                    refresh_token=SecretStr(payload.refresh_token),
                )
            )
            return CodexTokenSnapshot(revision=record.revision, tokens=tokens)

    def _replace(self, tokens: CodexTokens | None, expected_revision: int | None = None) -> bool:
        with self._locked():
            current = self._read()
            if expected_revision is not None and current.revision != expected_revision:
                return False
            payload = (
                None
                if tokens is None
                else _StoredTokenPayload(
                    id_token=tokens.id_token.get_secret_value(),
                    access_token=tokens.access_token.get_secret_value(),
                    refresh_token=tokens.refresh_token.get_secret_value(),
                )
            )
            serialized = _StoredSnapshot(revision=current.revision + 1, tokens=payload).model_dump_json()
            keyring.set_password(self._service, self._account, serialized)
            return True
