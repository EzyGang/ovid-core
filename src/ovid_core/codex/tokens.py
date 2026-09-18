import asyncio
import base64
import binascii
import time
from abc import abstractmethod
from collections.abc import Mapping
from typing import Literal, Protocol, cast

import httpx
from pydantic import Field, JsonValue, SecretStr, TypeAdapter, ValidationError

from ovid_core.codex.models import CodexOAuthConfig, CodexTokens, CodexTokenSnapshot
from ovid_core.errors import CodexAuthError
from ovid_core.models import BaseModel


_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])
_AUTH_CLAIM = 'https://api.openai.com/auth'
_JSON_HEADERS = httpx.Headers({'Content-Type': 'application/json'})


class CodexTokenStore(Protocol):
    @abstractmethod
    async def load(self) -> CodexTokens | None: ...

    @abstractmethod
    async def save(self, tokens: CodexTokens) -> None: ...

    @abstractmethod
    async def delete(self) -> None: ...

    @abstractmethod
    async def snapshot(self) -> CodexTokenSnapshot: ...

    @abstractmethod
    async def compare_and_swap(self, expected_revision: int, tokens: CodexTokens) -> bool: ...


class MemoryCodexTokenStore:
    def __init__(self) -> None:
        self._tokens: CodexTokens | None = None
        self._revision = 0

    async def load(self) -> CodexTokens | None:
        return self._tokens

    async def save(self, tokens: CodexTokens) -> None:
        self._tokens = tokens
        self._revision += 1

    async def delete(self) -> None:
        self._tokens = None
        self._revision += 1

    async def snapshot(self) -> CodexTokenSnapshot:
        return CodexTokenSnapshot(revision=self._revision, tokens=self._tokens)

    async def compare_and_swap(self, expected_revision: int, tokens: CodexTokens) -> bool:
        if self._revision != expected_revision:
            return False
        self._tokens = tokens
        self._revision += 1
        return True


class _RefreshResponse(BaseModel):
    id_token: SecretStr | None = None
    access_token: SecretStr | None = None
    refresh_token: SecretStr | None = None


class _RefreshRequest(BaseModel):
    client_id: str = Field(min_length=1)
    grant_type: Literal['refresh_token'] = 'refresh_token'
    refresh_token: str = Field(min_length=1, repr=False)


class _CodexTokenManager:
    def __init__(self, *, store: CodexTokenStore, http_client: httpx.AsyncClient, config: CodexOAuthConfig) -> None:
        self._store = store
        self._client = http_client
        self._config = config
        self._lock = asyncio.Lock()

    async def tokens(self, *, force_refresh: bool = False) -> CodexTokens:
        async with self._lock:
            snapshot = await self._store.snapshot()
            tokens = snapshot.tokens
            if tokens is None:
                raise CodexAuthError('Codex subscription authentication is required')
            if force_refresh or _expires_soon(tokens.access_token, self._config.refresh_window_seconds):
                updated = await self._refresh(tokens)
                if await self._store.compare_and_swap(snapshot.revision, updated):
                    return updated
                tokens = (await self._store.snapshot()).tokens
                if tokens is None:
                    raise CodexAuthError('Codex subscription authentication is required')
                if _expires_soon(tokens.access_token, self._config.refresh_window_seconds):
                    raise CodexAuthError('Codex credentials changed during refresh')
            return tokens

    async def snapshot(self) -> CodexTokenSnapshot:
        return await self._store.snapshot()

    async def save(self, *, expected_revision: int, tokens: CodexTokens) -> None:
        commit = asyncio.create_task(self._store.compare_and_swap(expected_revision, tokens))
        try:
            saved = await asyncio.shield(commit)
        except asyncio.CancelledError:
            await commit
            raise
        if not saved:
            raise CodexAuthError('Codex login was superseded')

    async def logout(self) -> None:
        await self._store.delete()

    async def _refresh(self, tokens: CodexTokens) -> CodexTokens:
        try:
            request = _RefreshRequest(
                client_id=self._config.client_id,
                refresh_token=tokens.refresh_token.get_secret_value(),
            )
            response = await self._client.post(
                f'{self._config.issuer.rstrip("/")}/oauth/token',
                content=request.model_dump_json(),
                headers=_JSON_HEADERS,
            )
            if not response.is_success:
                raise CodexAuthError(f'Codex token refresh failed with status {response.status_code}')
            refreshed = _RefreshResponse.model_validate_json(response.content, extra='ignore')
        except httpx.HTTPError, ValidationError:
            raise CodexAuthError('Codex token refresh failed') from None

        updated = CodexTokens(
            id_token=refreshed.id_token or tokens.id_token,
            access_token=refreshed.access_token or tokens.access_token,
            refresh_token=refreshed.refresh_token or tokens.refresh_token,
        )

        return updated


def codex_account_id(tokens: CodexTokens) -> str:
    claims = _jwt_claims(tokens.id_token)
    auth = claims.get(_AUTH_CLAIM)
    if not isinstance(auth, Mapping):
        raise CodexAuthError('Codex identity token does not contain an account')

    account_id = auth.get('chatgpt_account_id')
    if not isinstance(account_id, str) or not account_id:
        raise CodexAuthError('Codex identity token does not contain an account')

    return account_id


def _expires_soon(token: SecretStr, refresh_window_seconds: float) -> bool:
    claims = _jwt_claims(token)
    expiration = claims.get('exp')
    if not isinstance(expiration, int) or isinstance(expiration, bool):
        raise CodexAuthError('Codex access token does not contain an expiration')

    return expiration <= time.time() + refresh_window_seconds


def _jwt_claims(token: SecretStr) -> dict[str, JsonValue]:
    try:
        payload = token.get_secret_value().split('.')[1]
        decoded = base64.urlsafe_b64decode(f'{payload}{"=" * (-len(payload) % 4)}')

        return cast(dict[str, JsonValue], _JSON_OBJECT_ADAPTER.validate_json(decoded))
    except binascii.Error, IndexError, ValueError, ValidationError:
        raise CodexAuthError('Codex token is malformed') from None
