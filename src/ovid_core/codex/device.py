import asyncio
import time
from urllib.parse import urljoin

import httpx
from pydantic import ValidationError

from ovid_core.codex.models import (
    CodexDeviceAuthorization,
    CodexOAuthConfig,
    CodexTokens,
    _AuthorizationCodeExchangeRequest,
    _DeviceTokenRequest,
    _DeviceTokenResponse,
    _OAuthTokenResponse,
    _UserCodeRequest,
    _UserCodeResponse,
)
from ovid_core.codex.tokens import CodexTokenManager
from ovid_core.errors import CodexAuthError


_JSON_HEADERS = httpx.Headers({'Content-Type': 'application/json'})
_FORM_HEADERS = httpx.Headers({'Content-Type': 'application/x-www-form-urlencoded'})


class CodexDeviceAuthClient:
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        token_manager: CodexTokenManager,
        config: CodexOAuthConfig,
    ) -> None:
        self._client = http_client
        self._tokens = token_manager
        self._config = config

    async def start(self) -> CodexDeviceAuthorization:
        endpoint = f'{self._config.issuer.rstrip("/")}/api/accounts/deviceauth/usercode'
        try:
            request = _UserCodeRequest(client_id=self._config.client_id)
            response = await self._client.post(endpoint, content=request.model_dump_json(), headers=_JSON_HEADERS)
            if not response.is_success:
                raise CodexAuthError(f'Codex device authorization failed with status {response.status_code}')
            payload = _UserCodeResponse.model_validate_json(response.content, extra='ignore')
            interval = float(payload.interval)
            if interval <= 0:
                raise ValueError
        except httpx.HTTPError, ValidationError, ValueError:
            raise CodexAuthError('Codex device authorization failed') from None

        return CodexDeviceAuthorization(
            verification_url=f'{self._config.issuer.rstrip("/")}/codex/device',
            user_code=payload.user_code,
            device_auth_id=payload.device_auth_id,
            interval_seconds=interval,
        )

    async def complete(self, authorization: CodexDeviceAuthorization) -> CodexTokens:
        device_tokens = await self._poll(authorization)
        try:
            request = _AuthorizationCodeExchangeRequest(
                code=device_tokens.authorization_code,
                redirect_uri=urljoin(self._config.issuer, '/deviceauth/callback'),
                client_id=self._config.client_id,
                code_verifier=device_tokens.code_verifier,
            )
            response = await self._client.post(
                f'{self._config.issuer.rstrip("/")}/oauth/token',
                data=request.model_dump(mode='json'),
                headers=_FORM_HEADERS,
            )
            if not response.is_success:
                raise CodexAuthError(f'Codex token exchange failed with status {response.status_code}')
            payload = _OAuthTokenResponse.model_validate_json(response.content, extra='ignore')
        except httpx.HTTPError, ValidationError:
            raise CodexAuthError('Codex token exchange failed') from None

        tokens = CodexTokens(
            id_token=payload.id_token,
            access_token=payload.access_token,
            refresh_token=payload.refresh_token,
        )
        await self._tokens.save(tokens)

        return tokens

    async def _poll(self, authorization: CodexDeviceAuthorization) -> _DeviceTokenResponse:
        endpoint = f'{self._config.issuer.rstrip("/")}/api/accounts/deviceauth/token'
        deadline = time.monotonic() + self._config.poll_timeout_seconds
        while time.monotonic() < deadline:
            try:
                request = _DeviceTokenRequest(
                    device_auth_id=authorization.device_auth_id.get_secret_value(),
                    user_code=authorization.user_code,
                )
                response = await self._client.post(
                    endpoint,
                    content=request.model_dump_json(),
                    headers=_JSON_HEADERS,
                )
                if response.is_success:
                    return _DeviceTokenResponse.model_validate_json(response.content, extra='ignore')
                if response.status_code not in {403, 404}:
                    raise CodexAuthError(f'Codex device authorization failed with status {response.status_code}')
            except httpx.HTTPError, ValidationError:
                raise CodexAuthError('Codex device authorization failed') from None

            await asyncio.sleep(min(authorization.interval_seconds, max(0, deadline - time.monotonic())))

        raise CodexAuthError('Codex device authorization timed out')
