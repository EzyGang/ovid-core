import asyncio
import math
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from pydantic import ValidationError

from ovid_core.codex._oauth import exchange_authorization_code
from ovid_core.codex.models import (
    CodexOAuthConfig,
    CodexTokens,
    _DeviceTokenRequest,
    _DeviceTokenResponse,
    _UserCodeRequest,
    _UserCodeResponse,
)
from ovid_core.errors import CodexAuthError


_JSON_HEADERS = httpx.Headers({'Content-Type': 'application/json'})


@dataclass(frozen=True, slots=True)
class _DeviceAuthorization:
    verification_url: str
    user_code: str
    device_auth_id: str
    interval_seconds: float


class _DeviceLoginFlow:
    def __init__(self, *, http_client: httpx.AsyncClient, config: CodexOAuthConfig) -> None:
        self._client = http_client
        self._config = config

    async def start(self) -> _DeviceAuthorization:
        endpoint = f'{self._config.issuer.rstrip("/")}/api/accounts/deviceauth/usercode'
        try:
            request = _UserCodeRequest(client_id=self._config.client_id)
            response = await self._client.post(endpoint, content=request.model_dump_json(), headers=_JSON_HEADERS)
            if not response.is_success:
                raise CodexAuthError(f'Codex device authorization failed with status {response.status_code}')
            payload = _UserCodeResponse.model_validate_json(response.content, extra='ignore')
            interval = float(payload.interval)
            if not math.isfinite(interval) or interval <= 0:
                raise ValueError
        except httpx.HTTPError, ValidationError, ValueError:
            raise CodexAuthError('Codex device authorization failed') from None

        return _DeviceAuthorization(
            verification_url=f'{self._config.issuer.rstrip("/")}/codex/device',
            user_code=payload.user_code,
            device_auth_id=payload.device_auth_id,
            interval_seconds=interval,
        )

    async def complete(self, authorization: _DeviceAuthorization) -> CodexTokens:
        device_tokens = await self._poll(authorization)

        return await exchange_authorization_code(
            http_client=self._client,
            config=self._config,
            code=device_tokens.authorization_code,
            redirect_uri=urljoin(self._config.issuer, '/deviceauth/callback'),
            code_verifier=device_tokens.code_verifier,
        )

    async def _poll(self, authorization: _DeviceAuthorization) -> _DeviceTokenResponse:
        endpoint = f'{self._config.issuer.rstrip("/")}/api/accounts/deviceauth/token'
        while True:
            response = await self._poll_once(endpoint=endpoint, authorization=authorization)
            if response is not None:
                return response

            await asyncio.sleep(authorization.interval_seconds)

    async def _poll_once(self, *, endpoint: str, authorization: _DeviceAuthorization) -> _DeviceTokenResponse | None:
        try:
            request = _DeviceTokenRequest(
                device_auth_id=authorization.device_auth_id,
                user_code=authorization.user_code,
            )
            response = await self._client.post(endpoint, content=request.model_dump_json(), headers=_JSON_HEADERS)
            if response.is_success:
                return _DeviceTokenResponse.model_validate_json(response.content, extra='ignore')
            if response.status_code not in {403, 404}:
                raise CodexAuthError(f'Codex device authorization failed with status {response.status_code}')
        except httpx.HTTPError, ValidationError:
            raise CodexAuthError('Codex device authorization failed') from None

        return None
