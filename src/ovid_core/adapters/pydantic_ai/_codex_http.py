import json
import re
from collections.abc import AsyncGenerator

import httpx2

from ovid_core.codex.auth import CodexAuth
from ovid_core.codex.models import CodexTokens
from ovid_core.codex.tokens import codex_account_id
from ovid_core.models import BaseModel


_KEPT_HEADERS = frozenset({'retry-after', 'retry-after-ms', 'x-request-id'})
_KEPT_HEADER_PREFIX = 'x-ratelimit-'
_SECRET = re.compile(r'(?i)\bbearer\s+[^\s"\',]+|\b(?:[\w-]+\.)?eyJ[\w-]+\.[\w-]+|\bsk-[\w-]+')
_MAX_TEXT = 500


class _CodexErrorDetail(BaseModel):
    code: str | None = None
    type: str | None = None
    message: str


class _CodexErrorBody(BaseModel):
    error: _CodexErrorDetail


class _RedactingTransport(httpx2.AsyncBaseTransport):
    def __init__(self, wrapped: httpx2.AsyncBaseTransport) -> None:
        self._wrapped = wrapped

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        try:
            response = await self._wrapped.handle_async_request(request)
        except httpx2.RequestError as error:
            raise type(error)('Codex backend request failed', request=request) from None
        if response.status_code < 400:
            return response

        content = await response.aread()
        await response.aclose()
        headers = {
            key: value
            for key, value in response.headers.items()
            if key in _KEPT_HEADERS or key.startswith(_KEPT_HEADER_PREFIX)
        }
        body = _sanitized_body(content, status_code=response.status_code)

        return httpx2.Response(
            status_code=response.status_code,
            content=body.model_dump_json(exclude_none=True),
            headers={**headers, 'content-type': 'application/json'},
            request=request,
        )

    async def aclose(self) -> None:
        await self._wrapped.aclose()


def _sanitized_body(content: bytes, *, status_code: int) -> _CodexErrorBody:
    try:
        payload = json.loads(content)
    except ValueError:
        payload = None
    error = payload.get('error') if isinstance(payload, dict) else None
    if isinstance(error, str):
        error = {'message': error}
    fallback = f'Codex backend returned a {status_code // 100}xx status'
    if not isinstance(error, dict):
        return _CodexErrorBody(error=_CodexErrorDetail(message=fallback))

    code, error_type, message = (error.get(key) for key in ('code', 'type', 'message'))

    return _CodexErrorBody(
        error=_CodexErrorDetail(
            code=_scrubbed(code) if isinstance(code, str) else None,
            type=_scrubbed(error_type) if isinstance(error_type, str) else None,
            message=_scrubbed(message) if isinstance(message, str) else fallback,
        )
    )


def _scrubbed(text: str) -> str:
    return _SECRET.sub('[redacted]', text)[:_MAX_TEXT]


class _CodexHttpxAuth(httpx2.Auth):
    def __init__(self, auth: CodexAuth) -> None:
        self._auth = auth

    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        tokens = await self._auth._request_tokens()
        response = yield _prepare_request(request, tokens)
        if response.status_code == 401:
            await response.aread()
            tokens = await self._auth._request_tokens(force_refresh=True)
            yield _prepare_request(request, tokens)


def _prepare_request(request: httpx2.Request, tokens: CodexTokens) -> httpx2.Request:
    request.headers.pop('x-api-key', None)
    request.headers['authorization'] = f'Bearer {tokens.access_token.get_secret_value()}'
    request.headers['chatgpt-account-id'] = codex_account_id(tokens)
    request.headers['openai-beta'] = 'responses=experimental'
    request.headers['originator'] = 'ovid_core'
    return request
