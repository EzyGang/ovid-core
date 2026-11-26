import httpx2
import pytest
from pydantic import JsonValue

from ovid_core.adapters.pydantic_ai._codex_http import _RedactingTransport


async def _sanitized(response: httpx2.Response) -> httpx2.Response:
    transport = _RedactingTransport(httpx2.MockTransport(lambda _request: response))
    sanitized = await transport.handle_async_request(httpx2.Request('POST', 'https://chatgpt.example/responses'))
    await sanitized.aread()
    await transport.aclose()

    return sanitized


@pytest.mark.parametrize(
    ('content', 'expected'),
    [
        (b'<html>gateway-secret</html>', {'message': 'Codex backend returned a 5xx status'}),
        (b'["gateway-secret"]', {'message': 'Codex backend returned a 5xx status'}),
        (b'{"error": "plain failure"}', {'message': 'plain failure'}),
        (
            b'{"error": {"code": 7, "type": "server_error"}}',
            {'type': 'server_error', 'message': 'Codex backend returned a 5xx status'},
        ),
        (
            b'{"error": {"code": "server_error", "message": "token sk-live-abc123", "param": "gateway-secret"}}',
            {'code': 'server_error', 'message': 'token [redacted]'},
        ),
    ],
)
async def test_error_bodies_keep_only_sanitized_error_fields(content: bytes, expected: dict[str, JsonValue]) -> None:
    sanitized = await _sanitized(httpx2.Response(502, content=content))

    assert sanitized.status_code == 502
    assert sanitized.json() == {'error': expected}
    assert 'gateway-secret' not in sanitized.text


async def test_error_messages_are_truncated_after_scrubbing() -> None:
    message = f'Bearer secret-token {"x" * 600}'

    sanitized = await _sanitized(httpx2.Response(400, json={'error': {'message': message}}))

    assert sanitized.json()['error']['message'] == f'[redacted] {"x" * 600}'[:500]


async def test_error_responses_keep_only_allowlisted_headers() -> None:
    headers = {
        'Retry-After': '2',
        'retry-after-ms': '2000',
        'x-request-id': 'req_1',
        'x-ratelimit-remaining-requests': '0',
        'content-type': 'text/html',
        'set-cookie': 'session=secret',
    }

    sanitized = await _sanitized(httpx2.Response(429, content=b'busy', headers=headers))

    assert dict(sanitized.headers) == {
        'retry-after': '2',
        'retry-after-ms': '2000',
        'x-request-id': 'req_1',
        'x-ratelimit-remaining-requests': '0',
        'content-type': 'application/json',
        'content-length': str(len(sanitized.content)),
    }


async def test_transport_errors_keep_their_class_without_details() -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectTimeout('signed-url-secret', request=request)

    transport = _RedactingTransport(httpx2.MockTransport(fail))
    with pytest.raises(httpx2.ConnectTimeout) as captured:
        await transport.handle_async_request(httpx2.Request('POST', 'https://chatgpt.example/responses'))
    await transport.aclose()

    assert str(captured.value) == 'Codex backend request failed'
    assert captured.value.__cause__ is None
