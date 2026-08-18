from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast
from urllib.parse import parse_qs, urlencode, urlsplit

from ovid_core.codex.models import CodexOAuthConfig
from ovid_core.errors import CodexAuthError


_CALLBACK_PATH = '/auth/callback'
_SCOPE = 'openid profile email offline_access api.connectors.read api.connectors.invoke'


@dataclass(frozen=True, slots=True)
class BrowserCallbackSubmission:
    code: str
    response: asyncio.Future[tuple[int, str]]

    def finish(self, *, success: bool) -> None:

        status = 200 if success else 500
        message = (
            'Login complete. You can close this window.' if success else 'Login failed. Return to the application.'
        )
        self.response.set_result((status, message))


class BrowserCallbackServer:
    def __init__(
        self,
        *,
        server: asyncio.Server,
        config: CodexOAuthConfig,
        state: str,
        code_verifier: str,
        port: int,
    ) -> None:
        self._server = server
        self._state = state
        self._submissions: asyncio.Queue[BrowserCallbackSubmission | CodexAuthError] = asyncio.Queue()
        self._pending: set[asyncio.Future[tuple[int, str]]] = set()
        self.redirect_uri = f'http://localhost:{port}{_CALLBACK_PATH}'
        self.code_verifier = code_verifier
        self.authorization_url = _authorization_url(
            config=config,
            redirect_uri=self.redirect_uri,
            state=state,
            code_verifier=code_verifier,
        )

    @classmethod
    async def start(cls, *, config: CodexOAuthConfig) -> BrowserCallbackServer:
        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        holder: dict[str, BrowserCallbackServer] = {}

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await holder['callback']._handle(reader, writer)

        server = await _bind_server(handle=handle, ports=config.callback_ports)
        socket = server.sockets[0]
        port = cast(tuple[str, int], socket.getsockname())[1]
        callback = cls(server=server, config=config, state=state, code_verifier=code_verifier, port=port)
        holder['callback'] = callback

        return callback

    async def next_submission(self, *, timeout_seconds: float) -> BrowserCallbackSubmission:
        try:
            async with asyncio.timeout(timeout_seconds):
                result = await self._submissions.get()
        except TimeoutError:
            raise CodexAuthError('Codex browser login timed out') from None

        if isinstance(result, CodexAuthError):
            raise result

        return result

    async def close(self) -> None:

        self._server.close()
        await self._server.wait_closed()
        for response in self._pending:
            if not response.done():
                response.set_result((503, 'Login cancelled. Return to the application.'))
        self._submissions.put_nowait(CodexAuthError('Codex login was cancelled'))

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            target = await _request_target(reader)
            status, message = await self._process_target(target)
        except ValueError:
            status, message = 400, 'Invalid login callback.'

        await _write_response(writer, status=status, message=message)

    async def _process_target(self, target: str) -> tuple[int, str]:
        parsed = urlsplit(target)
        if parsed.path != _CALLBACK_PATH:
            return 404, 'Not found.'

        query = parse_qs(parsed.query)
        state = _single_value(query, 'state')
        if state is None or not secrets.compare_digest(state, self._state):
            return 400, 'Invalid login state.'
        if _single_value(query, 'error') is not None:
            self._submissions.put_nowait(CodexAuthError('Codex browser login was rejected'))
            return 400, 'Login was rejected. Return to the application.'

        code = _single_value(query, 'code')
        if code is None:
            self._submissions.put_nowait(CodexAuthError('Codex browser login did not return an authorization code'))
            return 400, 'Missing authorization code.'

        response = asyncio.get_running_loop().create_future()
        self._pending.add(response)
        self._submissions.put_nowait(BrowserCallbackSubmission(code=code, response=response))
        try:
            return await response
        finally:
            self._pending.discard(response)


def _authorization_url(*, config: CodexOAuthConfig, redirect_uri: str, state: str, code_verifier: str) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).decode().rstrip('=')
    query = urlencode(
        {
            'response_type': 'code',
            'client_id': config.client_id,
            'redirect_uri': redirect_uri,
            'scope': _SCOPE,
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
            'id_token_add_organizations': 'true',
            'codex_cli_simplified_flow': 'true',
            'state': state,
            'originator': 'ovid_core',
        }
    )

    return f'{config.issuer.rstrip("/")}/oauth/authorize?{query}'


async def _bind_server(
    *, handle: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]], ports: tuple[int, ...]
) -> asyncio.Server:
    for port in ports:
        try:
            return await asyncio.start_server(handle, host='127.0.0.1', port=port, limit=8192)
        except OSError:
            continue

    raise CodexAuthError('Codex browser login could not start its local callback server')


async def _request_target(reader: asyncio.StreamReader) -> str:
    request_line = (await reader.readline()).decode('ascii')
    method, target, version = request_line.rstrip('\r\n').split(' ')
    if method != 'GET' or version not in {'HTTP/1.0', 'HTTP/1.1'}:
        raise ValueError

    while True:
        header = await reader.readline()
        if header in {b'', b'\n', b'\r\n'}:
            break

    return target


async def _write_response(writer: asyncio.StreamWriter, *, status: int, message: str) -> None:
    reason = {200: 'OK', 400: 'Bad Request', 404: 'Not Found', 500: 'Internal Server Error', 503: 'Unavailable'}[status]
    body = message.encode()
    headers = (
        f'HTTP/1.1 {status} {reason}\r\n'
        'Content-Type: text/plain; charset=utf-8\r\n'
        f'Content-Length: {len(body)}\r\n'
        'Connection: close\r\n\r\n'
    ).encode()
    writer.write(headers + body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


def _single_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)

    return values[0] if values and values[0] else None
