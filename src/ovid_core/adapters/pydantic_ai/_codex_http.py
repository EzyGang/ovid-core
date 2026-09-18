from collections.abc import AsyncGenerator

import httpx

from ovid_core.codex.auth import CodexAuth
from ovid_core.codex.models import CodexTokens
from ovid_core.codex.tokens import codex_account_id


class _RedactingTransport(httpx.AsyncBaseTransport):
    def __init__(self, wrapped: httpx.AsyncBaseTransport) -> None:
        self._wrapped = wrapped

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            response = await self._wrapped.handle_async_request(request)
        except httpx.HTTPError:
            raise httpx.TransportError('Codex backend request failed') from None
        if response.status_code < 400:
            return response

        await response.aread()
        await response.aclose()
        return httpx.Response(
            status_code=response.status_code,
            content=b'{"error":"Codex backend request failed"}',
            headers={'content-type': 'application/json'},
            request=request,
        )

    async def aclose(self) -> None:
        await self._wrapped.aclose()


class _CodexHttpxAuth(httpx.Auth):
    def __init__(self, auth: CodexAuth) -> None:
        self._auth = auth

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        tokens = await self._auth._request_tokens()
        response = yield _prepare_request(request, tokens)
        if response.status_code == 401:
            await response.aread()
            tokens = await self._auth._request_tokens(force_refresh=True)
            yield _prepare_request(request, tokens)


def _prepare_request(request: httpx.Request, tokens: CodexTokens) -> httpx.Request:
    request.headers.pop('x-api-key', None)
    request.headers['authorization'] = f'Bearer {tokens.access_token.get_secret_value()}'
    request.headers['chatgpt-account-id'] = codex_account_id(tokens)
    request.headers['openai-beta'] = 'responses=experimental'
    request.headers['originator'] = 'ovid_core'
    return request
