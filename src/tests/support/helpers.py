import base64
import json
import time
from collections.abc import Callable
from uuid import UUID

import httpx
from pydantic import JsonValue, SecretStr, TypeAdapter

from ovid_core.codex import CodexTokens
from ovid_core.runtime import ConversationId, RunId
from ovid_core.usage import RequestUsage


RUN_ID = RunId(UUID('00000000-0000-0000-0000-000000000001'))
CONVERSATION_ID = ConversationId(UUID('00000000-0000-0000-0000-000000000002'))


_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


class MemoryTokenStore:
    def __init__(self, tokens: CodexTokens | None = None) -> None:
        self.value = tokens

    async def load(self) -> CodexTokens | None:
        return self.value

    async def save(self, tokens: CodexTokens) -> None:
        self.value = tokens

    async def delete(self) -> None:
        self.value = None


def make_jwt(payload: dict[str, JsonValue]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return f'header.{encoded}.signature'


def make_codex_tokens(*, expired: bool = False, suffix: str = 'old') -> CodexTokens:
    expires_at = int(time.time()) - 1 if expired else int(time.time()) + 3600
    return CodexTokens(
        id_token=SecretStr(
            make_jwt({'https://api.openai.com/auth': {'chatgpt_account_id': 'account-1'}, 'token': suffix})
        ),
        access_token=SecretStr(make_jwt({'exp': expires_at, 'token': suffix})),
        refresh_token=SecretStr(f'refresh-{suffix}'),
    )


def json_body(request: httpx.Request) -> dict[str, JsonValue]:
    return _JSON_OBJECT_ADAPTER.validate_json(request.content)


def oauth_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def make_request_usage(
    *,
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> RequestUsage:
    return RequestUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )
